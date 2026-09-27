"""调用 OpenAI 兼容的 ``/chat/completions``。

**为什么不引入 openai SDK。** 这个接口就是一次 POST 加一层 JSON，用标准库的 ``urllib``
十几行就能发。为它拉一个依赖进来，等于让所有用户都多装一个包 —— 而可换的端点（DeepSeek、
OpenAI、本地 Ollama、各种中转）本来就用的是同一个形状。

**这个类不碰界面。** 它是纯同步的，界面那边扔进线程跑。分开的好处是：出错路径、消息拼装
这些最容易写错的地方，可以**脱离 Qt 单独测**。

**出错要翻译成人话。** 用户看到 "HTTP 401" 不知道该干什么；看到"API Key 不对或者过期了，
去「编辑 → AI 设置」看一眼"就知道下一步。这个映射是这一层最主要的价值。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .ai_panel import AiMessage
from .ai_settings import AiSettings, resolve_api_key

__all__ = ["AiError", "ChatBackend"]


class AiError(RuntimeError):
    """调用模型失败。消息**直接就是给用户看的**，不要再包一层。"""


class ChatBackend:
    """一个 OpenAI 兼容的对话后端。可调用对象，签名跟 ``AiBackend`` 协议一致。"""

    def __init__(self, settings: AiSettings) -> None:
        self.settings = settings

    # -- 组装 ----------------------------------------------------------------

    def messages(self, prompt: str, history: list[AiMessage]) -> list[dict[str, str]]:
        """把对话历史拼成接口要的形状。

        **不要再把 ``prompt`` 追加到末尾。** ``AiPanel`` 是先把用户这句话记进历史、再调
        后端的，所以 ``history`` 的最后一条就是它 —— 再追加一次，同一句话会发两遍。
        这种"发重了"模型不会报错，只会答得莫名其妙，很难往回查。
        """
        out: list[dict[str, str]] = []
        system = (self.settings.system_prompt or "").strip()
        if system:
            out.append({"role": "system", "content": system})
        for message in history:
            if message.role in ("user", "assistant") and (message.text or "").strip():
                out.append({"role": message.role, "content": message.text})
        if not out or out[-1]["role"] != "user":
            # 兜底：历史被清过、或者调用方直接调后端而没走面板。
            out.append({"role": "user", "content": prompt})
        return out

    def payload(self, prompt: str, history: list[AiMessage]) -> dict[str, Any]:
        return {
            "model": self.settings.model or "deepseek-chat",
            "messages": self.messages(prompt, history),
            "temperature": float(self.settings.temperature),
            "stream": False,
        }

    # -- 调用 ----------------------------------------------------------------

    def __call__(self, prompt: str, history: list[AiMessage]) -> AiMessage:
        key = resolve_api_key(self.settings)
        if not key:
            raise AiError(
                "还没填 API Key。\n"
                "打开「编辑 → 设置…」填上，或者设一个环境变量 MYAUTOWORK_AI_KEY。"
            )

        url = self.settings.chat_url
        body = json.dumps(self.payload(prompt, history), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=float(self.settings.timeout)) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # pragma: no cover - 读不到就算了
                detail = ""
            raise AiError(self._http_hint(exc.code, url, detail)) from exc
        except urllib.error.URLError as exc:
            raise AiError(
                f"连不上 {url}。\n原因：{exc.reason}\n"
                "地址填对了吗？要连外网的话，代理开了吗？"
            ) from exc
        except TimeoutError as exc:
            raise AiError(
                f"等了 {self.settings.timeout:.0f} 秒还没回应。\n"
                "模型可能太忙，或者「超时」设得太短 —— 在「编辑 → 设置…」里调大。"
            ) from exc

        return self._parse(raw, url)

    # -- 解析 ----------------------------------------------------------------

    @staticmethod
    def _http_hint(code: int, url: str, detail: str) -> str:
        hints = {
            400: "请求本身有问题 —— 多半是模型名填错了。",
            401: "API Key 不对或者过期了，去「编辑 → 设置…」看一眼。",
            402: "账户余额不够了。",
            403: "这个 Key 没有访问该模型的权限。",
            404: "地址不对 —— 检查「接口地址」。很多服务要在后面带 /v1。",
            429: "请求太频繁或者额度用完了，等一会儿再试。",
            500: "对方服务器出错了，跟你的设置无关，过会儿再试。",
            502: "网关错误，多半是对方的临时问题。",
            503: "对方服务暂时不可用。",
        }
        hint = hints.get(code, "这个状态码不在常见清单里，看下面的原文。")
        text = f"接口返回 {code}：{hint}\n{url}"
        if detail:
            text += f"\n\n对方的原话：\n{detail}"
        return text

    @staticmethod
    def _parse(raw: str, url: str) -> AiMessage:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AiError(
                f"接口返回的不是 JSON，可能地址指到了别的服务。\n{url}\n"
                f"前 200 个字：{raw[:200]!r}"
            ) from exc

        if not isinstance(data, dict):
            raise AiError(f"接口返回的 JSON 不是对象：{raw[:200]!r}")

        # 有些兼容端点把错误包在 200 里面，不检查的话下面会以 KeyError 收场，
        # 那就把一个能说清的问题变成了一句"没看懂"。
        error = data.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise AiError(f"接口报了错：{message}")

        choices = data.get("choices") or []
        if not choices:
            raise AiError(f"接口没给出任何回复（没有 choices 字段）。原样返回：{raw[:300]}")

        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if not str(text).strip():
            finish = choices[0].get("finish_reason") or "未知"
            raise AiError(
                f"模型返回了空内容（结束原因：{finish}）。\n"
                "如果 finish_reason 是 length，说明回复被长度限制截断了。"
            )
        return AiMessage(role="assistant", text=str(text))
