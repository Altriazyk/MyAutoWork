"""参数与端口类型系统。

这个模块声明的东西同时决定三件事：

1. 画布上端口的颜色与形状
2. 连线时的类型校验（见 ``compatible``）
3. 属性面板自动生成的控件（见 ``Field.to_schema``）

所以它必须保持在最底层、不依赖任何其它模块 —— 插件作者、内核、界面三方都 import 它。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = [
    "NOTSET",
    "Field",
    "Any_",
    "String",
    "Number",
    "Integer",
    "Bool",
    "Text",
    "Code",
    "Enum",
    "Element",
    "File",
    "Folder",
    "Credential",
    "Color",
    "List_",
    "Dict_",
    "compatible",
    "coerce",
    "normalize_fields",
]

#: 区分"没有默认值"和"默认值是 None"。
class _NotSet:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return "NOTSET"

    def __bool__(self) -> bool:
        return False


NOTSET = _NotSet()

# 已知类型名。新增类型时必须同步更新这里，否则兼容性判断会静默走 "any" 分支。
KNOWN_KINDS = frozenset(
    {
        "any",
        "string",
        "text",
        "code",
        "number",
        "integer",
        "bool",
        "enum",
        "element",
        "file",
        "folder",
        "credential",
        "color",
        "list",
        "dict",
    }
)

# 文本类：本质就是一段字符串，彼此可以互转。
_STRINGY_KINDS = frozenset({"string", "text", "code", "color", "credential"})

# 数值类：可以互相提升。
_NUMERIC_KINDS = frozenset({"number", "integer"})

# 普通标量：互转成文本是日常操作（把数字拼进日志、把布尔写进文件名）。
_PLAIN_SCALAR_KINDS = _STRINGY_KINDS | _NUMERIC_KINDS | frozenset({"bool"})

# 路径类：本质是文本，所以能和文本互转，但**不能**和数字/布尔互转 ——
# 把 42 当成文件路径一定是连错了，应该连线的当下就拦住，而不是等运行时报错。
_PATHLIKE_KINDS = frozenset({"file", "folder"})


class Field:
    """一个输入/输出端口的声明。

    通常不直接实例化，用下面的 ``String()`` / ``Element()`` 等快捷构造更易读。
    """

    __slots__ = (
        "kind",
        "default",
        "required",
        "label",
        "help",
        "choices",
        "multiline",
        "picker",
        "placeholder",
        "minimum",
        "maximum",
        "lang",
        "item_kind",
        "secret",
    )

    def __init__(
        self,
        kind: str = "any",
        *,
        default: Any = NOTSET,
        required: bool = False,
        label: str | None = None,
        help: str | None = None,
        choices: Iterable[Any] | None = None,
        multiline: bool = False,
        picker: str | None = None,
        placeholder: str | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
        lang: str | None = None,
        item_kind: str | None = None,
        secret: bool = False,
    ) -> None:
        if kind not in KNOWN_KINDS:
            raise ValueError(f"未知的字段类型 {kind!r}，可选：{sorted(KNOWN_KINDS)}")
        self.kind = kind
        self.default = default
        self.required = bool(required)
        # 必填字段不给默认值，否则引擎校验会自相矛盾。
        if self.required and default is not NOTSET:
            raise ValueError(f"必填字段不能同时声明默认值（kind={kind}）")
        self.label = label
        self.help = help
        self.choices = list(choices) if choices is not None else None
        if self.kind == "enum" and not self.choices:
            raise ValueError("Enum 字段必须提供 choices")
        self.multiline = bool(multiline)
        self.picker = picker or ("file" if kind == "file" else "folder" if kind == "folder" else None)
        self.placeholder = placeholder
        self.minimum = minimum
        self.maximum = maximum
        self.lang = lang
        self.item_kind = item_kind
        self.secret = bool(secret)

    # -- 序列化 ---------------------------------------------------------------

    def to_schema(self) -> dict[str, Any]:
        """转成可 JSON 序列化的描述，交给界面去渲染控件。"""
        schema: dict[str, Any] = {"kind": self.kind, "required": self.required}
        for attr in (
            "label",
            "help",
            "choices",
            "multiline",
            "picker",
            "placeholder",
            "minimum",
            "maximum",
            "lang",
            "secret",
        ):
            value = getattr(self, attr)
            if value not in (None, False):
                schema[attr] = value
        if self.item_kind is not None:
            schema["item_kind"] = self.item_kind
        if self.default is not NOTSET:
            schema["default"] = self.default
        return schema

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        bits = [self.kind]
        if self.required:
            bits.append("required")
        if self.default is not NOTSET:
            bits.append(f"default={self.default!r}")
        return f"Field({' '.join(bits)})"


# --------------------------------------------------------------------------- #
# 快捷构造
# --------------------------------------------------------------------------- #


def Any_(**kw: Any) -> Field:
    return Field("any", **kw)


def String(default: Any = NOTSET, **kw: Any) -> Field:
    if default is not NOTSET:
        kw["default"] = default
    return Field("string", **kw)


def Number(default: Any = NOTSET, **kw: Any) -> Field:
    if default is not NOTSET:
        kw["default"] = default
    return Field("number", **kw)


def Integer(default: Any = NOTSET, **kw: Any) -> Field:
    if default is not NOTSET:
        kw["default"] = default
    return Field("integer", **kw)


def Bool(default: Any = NOTSET, **kw: Any) -> Field:
    if default is not NOTSET:
        kw["default"] = default
    return Field("bool", **kw)


def Text(default: Any = NOTSET, **kw: Any) -> Field:
    kw.setdefault("multiline", True)
    if default is not NOTSET:
        kw["default"] = default
    return Field("text", **kw)


def Code(lang: str = "python", default: Any = NOTSET, **kw: Any) -> Field:
    kw["lang"] = lang
    kw.setdefault("multiline", True)
    if default is not NOTSET:
        kw["default"] = default
    return Field("code", **kw)


def Enum(choices: Iterable[Any], default: Any = NOTSET, **kw: Any) -> Field:
    kw["choices"] = choices
    if default is not NOTSET:
        kw["default"] = default
    return Field("enum", **kw)


def Element(picker: str = "uia", **kw: Any) -> Field:
    """界面元素定位器。

    带 ``picker`` 的字段在属性面板上会自动多出一个 🎯 拾取按钮 —— 这是界面自动化
    能用的前提，因为用户不可能手写 AutomationId。
    """
    kw.setdefault("picker", picker)
    return Field("element", **kw)


def File(picker: str = "open", **kw: Any) -> Field:
    return Field("file", picker=picker, **kw)


def Folder(**kw: Any) -> Field:
    return Field("folder", picker="folder", **kw)


def Credential(**kw: Any) -> Field:
    kw.setdefault("secret", True)
    return Field("credential", **kw)


def Color(default: Any = NOTSET, **kw: Any) -> Field:
    if default is not NOTSET:
        kw["default"] = default
    return Field("color", **kw)


def List_(item_kind: str = "any", **kw: Any) -> Field:
    kw["item_kind"] = item_kind
    return Field("list", **kw)


def Dict_(**kw: Any) -> Field:
    return Field("dict", **kw)


# --------------------------------------------------------------------------- #
# 兼容性
# --------------------------------------------------------------------------- #


def compatible(src: str, dst: str) -> bool:
    """判断 ``src`` 类型的输出能不能连到 ``dst`` 类型的输入。

    刻意允许得比严格类型系统宽松一些：自动化流程里把数字拼进字符串、把路径当文本用
    都是日常操作。真正不兼容的是三类：

    - 数字/布尔 ↔ 路径（把 42 当文件路径一定是连错了）
    - 标量 ↔ 集合（list / dict）
    - 标量 ↔ 界面元素
    """
    if src == dst or src == "any" or dst == "any":
        return True
    if src in _NUMERIC_KINDS and dst in _NUMERIC_KINDS:
        return True
    if src in _PLAIN_SCALAR_KINDS and dst in _PLAIN_SCALAR_KINDS:
        return True
    if src in _PATHLIKE_KINDS and dst in _PATHLIKE_KINDS | _STRINGY_KINDS:
        return True
    if src in _STRINGY_KINDS and dst in _PATHLIKE_KINDS:
        return True
    return False


def coerce(value: Any, dst: str) -> Any:
    """按目标类型做一次温和的转换；转不了就原样返回，交给插件自己处理。"""
    if value is None:
        return None
    try:
        if dst == "string" or dst == "text" or dst == "code":
            return value if isinstance(value, str) else str(value)
        if dst == "number":
            return float(value)
        if dst == "integer":
            return int(value)
        if dst == "bool":
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on", "是"}
            return bool(value)
    except (TypeError, ValueError):
        return value
    return value


def normalize_fields(mapping: Mapping[str, Any] | None) -> dict[str, Field]:
    """把插件的 ``inputs={...}`` 声明规整成 ``{名字: Field}``。

    允许简写：``{"url": "string"}`` 等价于 ``{"url": String()}``。
    """
    if not mapping:
        return {}
    out: dict[str, Field] = {}
    for name, spec in mapping.items():
        if isinstance(spec, Field):
            out[name] = spec
        elif isinstance(spec, str):
            out[name] = Field(spec)
        elif isinstance(spec, Mapping):
            out[name] = Field(**spec)  # type: ignore[arg-type]
        else:
            raise TypeError(f"端口 {name!r} 的声明无法识别：{spec!r}")
    return out
