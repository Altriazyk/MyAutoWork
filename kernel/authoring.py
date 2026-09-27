"""插件草稿：把"生成一个插件"当成普通的数据操作。

AI 助手要能创建自定义模块，所以"写一个插件"不能是只有人才能做的事。这里把它抽出来：

- :class:`PluginDraft` 描述一个插件（id / 名称 / 分类 / 代码）
- :meth:`PluginDraft.write` 把它落成 ``plugins/<id>/manifest.json`` + ``main.py``
- :func:`plugin_template` 产出一份能直接跑起来的骨架代码

**为什么落盘要单独一步、而不是让调用方自己拼文件**：插件是会读写文件、会操作界面的代码。
把"校验 + 写入"收在一处，才能在写盘之前统一检查 id 合法性、main.py 能不能编译，避免
留下一个半坏的插件把整个注册表带崩。

放在 kernel 而不是 host，是因为它跟 Qt 无关：命令行、测试、以后的插件市场都能用。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from string import Template
from typing import Any

from .errors import MyAutoWorkError
from .manifest import API_VERSION, MANIFEST_FILENAME

__all__ = ["PluginDraft", "plugin_template", "PluginDraftError", "PLUGIN_ID_PATTERN"]

#: 插件 id 允许的字符。和 manifest 的校验规则保持一致。
PLUGIN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PluginDraftError(MyAutoWorkError):
    """草稿本身有问题，或者落盘失败。"""


_TEMPLATE = Template(
    '''"""$doc_title

$description_text
"""

from __future__ import annotations

from myautowork import Context, String, action


@action(
    id="$action_id",
    name=$name_literal,
    category=$category_literal,
    description=$description_literal,
    inputs={
        "text": String(default="", label="输入"),
    },
    outputs={
        "text": String(label="输出"),
    },
)
def $action_id(ctx: Context, text: str = "") -> dict:
    """$description_text"""
    ctx.info("收到：%s" % text)
    return {"text": text}
'''
)


def _doc_safe(text: str) -> str:
    """放进三引号字符串里要安全：把 ``\"\"\"`` 换掉，反斜杠转义掉。"""
    return text.replace('"""', "'''").replace("\\", "\\\\")


def plugin_template(
    *,
    plugin_id: str,
    name: str,
    description: str = "",
    category: str = "通用",
    action_id: str = "run",
) -> str:
    """生成一份能直接跑起来的插件骨架代码。

    故意写得完整可运行（有装饰器、有输入输出声明、有返回值），因为这份代码既是 AI 的
    起点，也是"新建插件"的起点 —— 一个跑不起来的骨架只会让人先去修它。
    """
    safe_action = re.sub(r"\W", "_", action_id).strip("_") or "run"
    summary = description or name
    return _TEMPLATE.substitute(
        action_id=safe_action,
        name_literal=json.dumps(name, ensure_ascii=False),
        category_literal=json.dumps(category, ensure_ascii=False),
        description_literal=json.dumps(summary, ensure_ascii=False),
        doc_title=_doc_safe(name),
        description_text=_doc_safe(summary),
    )


@dataclass
class PluginDraft:
    """一个还没落盘的插件。"""

    plugin_id: str
    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = "myautowork"
    category: str = "通用"
    keywords: list[str] = dc_field(default_factory=list)
    #: 需要的第三方包，pip 要求的写法。写进 manifest，界面上会检查装没装。
    dependencies: list[str] = dc_field(default_factory=list)
    code: str = ""
    #: 静态能力索引，形如 ``{"actions": ["run"], "triggers": []}``。
    provides: dict[str, list[str]] = dc_field(default_factory=dict)
    #: 生成这份草稿的来源说明（比如"来自 AI 助手的第 3 轮回答"），只用于展示。
    origin: str = ""

    # -- 校验 ----------------------------------------------------------------

    def validate(self) -> list[str]:
        """返回所有问题。空列表表示可以落盘。

        一次报全部问题，而不是遇到第一个就抛 —— 用户（或 AI）拿到完整清单才能一次改完。
        """
        problems: list[str] = []

        if not self.plugin_id:
            problems.append("插件 id 不能为空")
        elif not PLUGIN_ID_PATTERN.match(self.plugin_id):
            problems.append(
                f"插件 id {self.plugin_id!r} 含非法字符："
                "只允许字母、数字、点、下划线、连字符，且必须以字母或数字开头"
            )
        elif self.plugin_id in {".", ".."}:
            problems.append("插件 id 不能是 . 或 ..")

        if not self.name.strip():
            problems.append("插件名称不能为空")

        if not self.code.strip():
            problems.append("main.py 内容不能为空")
        else:
            try:
                ast.parse(self.code)
            except SyntaxError as exc:
                problems.append(f"main.py 语法错误：第 {exc.lineno} 行 {exc.msg}")

        for key in self.provides:
            if key not in ("actions", "triggers"):
                problems.append(f"provides 里不认识 {key!r}（只能是 actions / triggers）")

        return problems

    @property
    def ok(self) -> bool:
        return not self.validate()

    # -- 序列化 --------------------------------------------------------------

    def to_manifest(self) -> dict[str, Any]:
        return {
            "id": self.plugin_id,
            "name": self.name,
            "version": self.version,
            "api_version": API_VERSION,
            "description": self.description,
            "author": self.author,
            "category": self.category,
            "keywords": list(self.keywords),
            "dependencies": list(self.dependencies),
            "runtime": {"type": "python", "entry": "main.py"},
            "provides": {
                "actions": list(self.provides.get("actions") or []),
                "triggers": list(self.provides.get("triggers") or []),
            },
        }

    @property
    def directory_name(self) -> str:
        return self.plugin_id

    # -- 落盘 ----------------------------------------------------------------

    def write(self, plugins_dir: str | Path, *, overwrite: bool = False) -> Path:
        """写成一个真正的插件目录，返回它的路径。

        默认不覆盖已有插件：AI 生成的东西撞上用户手写的插件时，应该报错而不是悄悄盖掉。
        """
        problems = self.validate()
        if problems:
            raise PluginDraftError("插件草稿校验未通过：" + "；".join(problems))

        target = Path(plugins_dir) / self.directory_name
        if target.exists() and not overwrite:
            raise PluginDraftError(
                f"插件目录已存在：{target}。要覆盖请显式指定 overwrite=True。"
            )

        target.mkdir(parents=True, exist_ok=True)
        (target / MANIFEST_FILENAME).write_text(
            json.dumps(self.to_manifest(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (target / "main.py").write_text(self.code, encoding="utf-8")
        return target

    def summary(self) -> str:
        abilities = list(self.provides.get("actions") or []) + list(self.provides.get("triggers") or [])
        tail = f" · {len(abilities)} 个能力" if abilities else ""
        return f"{self.name}（{self.plugin_id}）v{self.version}{tail}"
