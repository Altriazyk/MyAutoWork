"""AI 助手的设置：接口地址、密钥、模型，以及**告诉模型 SDK 长什么样**的系统提示词。

**为什么存在用户目录而不是工作目录。** 这份文件里有 API Key。工作目录是用户放流程和插件
的地方，随手就能被压缩、备份、发给别人 —— 密钥不该跟着一起走。放 ``%APPDATA%`` 下，
跟程序数据分开。

**密钥是明文存的。** 这一点不打算掩饰：没有引入 keyring 之类的依赖，就是明文。设置面板上
会直接写明，让用户自己决定要不要填。想更安全的话，去环境变量里给，程序也认。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "AiSettings",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "SDK_BRIEF",
    "load_settings",
    "save_settings",
    "settings_path",
    "resolve_api_key",
]

#: 默认走 DeepSeek 的 OpenAI 兼容接口。换成任何兼容端点只要改地址和模型名。
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

#: 环境变量名。填了它就优先用环境变量里的密钥，文件里那份可以留空。
ENV_API_KEY = "MYAUTOWORK_AI_KEY"

#: 默认系统提示词：把插件的 SDK 讲清楚。
#: **这段是"SDK 设置"的真正价值所在** —— 模型不知道这套 SDK 长什么样，就只会写出一堆
#: 跑不起来的代码。把它作为可编辑的默认值，用户能按自己的需要增删。
SDK_BRIEF = """你是 myautowork 的自动化助手。这个程序用插件 + 流程节点来做自动化。

写插件时必须遵守：

1. 一个插件是一个目录，里面有 manifest.json 和 main.py。

2. manifest.json 的形状：
{
  "id": "作者.名字",
  "name": "中文名",
  "version": "0.1.0",
  "api_version": "1",
  "description": "一句话说明",
  "category": "分类",
  "runtime": {"type": "python", "entry": "main.py"},
  "provides": {"actions": ["动作id"], "triggers": []}
}
需要第三方包时加 "dependencies": ["包名"]，界面会检查装没装。

3. main.py 用 SDK 的 @action 装饰器注册动作：

from myautowork import Context, String, Integer, Bool, Enum, File, Folder, Text, Any_, action

@action(
    id="do_something",
    name="做点什么",
    category="分类",
    description="给用户看的一句话",
    inputs={
        "path": File(picker="open", required=True, label="文件", help="说明"),
        "count": Integer(default=1, label="次数"),
    },
    outputs={"result": String(label="结果")},
)
def do_something(ctx: Context, path: str, count: int = 1, **_):
    ctx.info("开始了")
    return {"result": "..."}

4. 可以用到的字段类型：String / Text / Integer / Number / Bool / Enum / File / Folder /
   Any_ / List_ / Dict_ / Credential / Color。File 和 Folder 支持 picker="open"/"save"。
   必填加 required=True。

5. 在动作里可以：
   - ctx.info / ctx.warning / ctx.error / ctx.debug 写日志
   - ctx.set_var("名字", 值) 写工作流变量，后面的节点用 {{ $vars.名字 }} 取
   - ctx.get_var("名字") 读回来
   - ctx.resolve("相对路径") 拿到工作目录下的绝对路径

6. 出错了就抛异常，引擎会走节点的 error 出口。**预期之内的失败**（条件不满足、没找到目标）
   抛 myautowork.ExpectedError，日志里只留一行；真正的 bug 直接抛普通异常，会打完整栈。

7. 参数的值有三种来源：常量、{{ }} 表达式（只做点分路径取值，比如 {{ $node.n3.text }}，
   **没有运算符**）、以及从别的节点的输出连线过来。

回答时先说思路，代码放在代码块里。用户要的是能直接用的东西，不要省略号。"""


@dataclass
class AiSettings:
    """AI 助手的全部可调项。"""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = 0.3
    timeout: float = 60.0
    system_prompt: str = SDK_BRIEF

    # -- 派生 -----------------------------------------------------------------

    @property
    def chat_url(self) -> str:
        """拼出 /chat/completions 的完整地址。

        用户填的地址可能是 ``https://x/v1``、``https://x/v1/`` 或者已经带上了
        ``/chat/completions`` —— 三种都认，免得为了一个斜杠让人反复试。
        """
        base = (self.base_url or "").strip().rstrip("/")
        if not base:
            base = DEFAULT_BASE_URL
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "AiSettings":
        if not isinstance(data, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        clean: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key in ("temperature", "timeout"):
                try:
                    clean[key] = float(value)
                except (TypeError, ValueError):
                    continue
            else:
                clean[key] = str(value)
        return cls(**clean)


def settings_path() -> Path:
    """设置文件放哪。用户目录下，跟工作目录分开。"""
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "myautowork" / "ai.json"


def load_settings(path: str | Path | None = None) -> AiSettings:
    """读设置。文件不在、或者读坏了，就返回默认值 —— **一个坏掉的配置文件不该让程序起不来**。"""
    target = Path(path) if path is not None else settings_path()
    if not target.is_file():
        return AiSettings()
    try:
        return AiSettings.from_dict(json.loads(target.read_text(encoding="utf-8")))
    except Exception:
        return AiSettings()


def save_settings(settings: AiSettings, path: str | Path | None = None) -> Path:
    target = Path(path) if path is not None else settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def resolve_api_key(settings: AiSettings) -> str:
    """密钥从哪来：**环境变量优先**。

    这样把密钥写进环境变量的人，不必再往明文文件里放一份。
    """
    return (os.environ.get(ENV_API_KEY) or settings.api_key or "").strip()
