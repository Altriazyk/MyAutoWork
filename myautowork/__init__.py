"""myautowork —— 插件 SDK。

插件作者只需要 import 这个包：

    from myautowork import action, trigger, Context, String, Number, Element, File

内核和界面不 import 这里的执行逻辑，只消费 ``describe_interface()`` 产出的
接口描述。所以 SDK 的公开面必须窄、稳定、可 JSON 序列化。

版本策略：``API_VERSION`` 变化意味着插件契约有破坏性变更。内核会拒绝加载
api_version 不匹配的插件，而不是让它在运行时炸掉。
"""

from __future__ import annotations

from .context import Context, LogSink
from .errors import ConditionNotMet, ExpectedError
from .decorators import (
    ActionSpec,
    TriggerSpec,
    action,
    clear_registry,
    describe_interface,
    registered_actions,
    registered_triggers,
    trigger,
)
from .fields import (
    NOTSET,
    Any_,
    Bool,
    Code,
    Color,
    Credential,
    Dict_,
    Element,
    Enum,
    Field,
    File,
    Folder,
    Integer,
    List_,
    Number,
    String,
    Text,
    coerce,
    compatible,
    normalize_fields,
)
from .locator import STRATEGY_LABELS, STRATEGY_ORDER, Locator, PathStep
from .lifecycle import on_cleanup, pending_cleanups, run_cleanups

#: 插件契约版本。内核只接受相同主版本号的插件。
API_VERSION = "1"

__version__ = "0.1.0"

__all__ = [
    "API_VERSION",
    "__version__",
    # 装饰器
    "action",
    "trigger",
    "ActionSpec",
    "TriggerSpec",
    "describe_interface",
    "registered_actions",
    "registered_triggers",
    "on_cleanup",
    "pending_cleanups",
    "run_cleanups",
    "clear_registry",
    # 上下文
    "Context",
    "LogSink",
    "ExpectedError",
    "ConditionNotMet",
    # 类型
    "Field",
    "NOTSET",
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
    # 界面元素定位（拾取器产出，win.uia 消费）
    "Locator",
    "PathStep",
    "STRATEGY_ORDER",
    "STRATEGY_LABELS",
]
