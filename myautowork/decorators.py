"""插件用这两个装饰器声明自己的能力。

    from myautowork import action, trigger, Context, String, Element

    @action(
        id="click",
        name="点击元素",
        category="界面",
        inputs={"element": Element(required=True)},
        outputs={"clicked": Bool()},
    )
    def click(ctx: Context, element):
        ...

装饰器里的声明**就是**界面看到的节点。插件加多少，界面代码一行都不用改 ——
这是整个插件化能滚起来的前提。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Mapping

from .fields import Field, normalize_fields

__all__ = [
    "ActionSpec",
    "TriggerSpec",
    "action",
    "trigger",
    "registered_actions",
    "registered_triggers",
    "describe_interface",
    "clear_registry",
]


@dataclass
class ActionSpec:
    """一个可被工作流节点调用的动作。"""

    id: str
    func: Callable[..., Any]
    name: str
    category: str = "通用"
    icon: str | None = None
    description: str = ""
    inputs: dict[str, Field] = dc_field(default_factory=dict)
    outputs: dict[str, Field] = dc_field(default_factory=dict)
    #: 是否阻塞型动作。界面自动化里大量操作会等待，界面可以据此提示。
    blocking: bool = True
    #: **分支出口由哪个参数决定。** 填参数名，那个参数的值（字符串列表）就是这个节点
    #: 的执行出口名。空字符串表示这个动作只有 success / error 两个出口。
    #:
    #: 为什么是"由参数决定"而不是写死：分支数量是用户定的 —— 三路、五路、十路。
    #: 静态声明做不到，而这个设计天然支持"在属性面板里加一条，画布上就多一个出口"，
    #: 和属性面板本身"schema 驱动"是同一个思路。
    branches: str = ""

    def to_schema(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "icon": self.icon,
            "description": self.description,
            "blocking": self.blocking,
            "branches": self.branches,
            "inputs": {k: v.to_schema() for k, v in self.inputs.items()},
            "outputs": {k: v.to_schema() for k, v in self.outputs.items()},
            "kind": "action",
        }


@dataclass
class TriggerSpec:
    """一个能启动工作流的来源。

    当前只有"手动触发"这种被内核直接调用的一次性触发器。定时 / 热键 / 文件变化这类
    长驻触发器后续会由内核启动并推送事件，所以这里保留 ``events`` 字段先把位置占住。
    """

    id: str
    func: Callable[..., Any]
    name: str
    category: str = "触发"
    icon: str | None = None
    description: str = ""
    inputs: dict[str, Field] = dc_field(default_factory=dict)
    outputs: dict[str, Field] = dc_field(default_factory=dict)
    events: tuple[str, ...] = ()
    #: True 表示由内核主动调用一次（手动触发）；False 表示长驻监听。
    one_shot: bool = True

    def to_schema(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "icon": self.icon,
            "description": self.description,
            "events": list(self.events),
            "one_shot": self.one_shot,
            "inputs": {k: v.to_schema() for k, v in self.inputs.items()},
            "outputs": {k: v.to_schema() for k, v in self.outputs.items()},
            "kind": "trigger",
        }


_ACTIONS: dict[str, ActionSpec] = {}
_TRIGGERS: dict[str, TriggerSpec] = {}


def _as_fields(mapping: Mapping[str, Any] | None) -> dict[str, Field]:
    return normalize_fields(mapping)


def action(
    id: str,
    name: str | None = None,
    *,
    category: str = "通用",
    icon: str | None = None,
    description: str = "",
    inputs: Mapping[str, Any] | None = None,
    outputs: Mapping[str, Any] | None = None,
    blocking: bool = True,
    branches: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个函数注册成工作流可用的动作。

    ``branches`` 填一个**输入参数名**，那个参数的值（字符串列表）就是这个节点多出来的
    执行出口。动作返回 ``{"branch": "某个出口名"}`` 就走到那一路上去。

    这样一条流程要分四路，是 1 个节点 + 4 条边，而不是 3 个串联的 if 节点 + 6 条边。
    """

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        if id in _ACTIONS:
            raise ValueError(f"动作 id 重复：{id!r}（已由 {_ACTIONS[id].func.__name__} 注册）")
        resolved_description = description
        if not resolved_description and func.__doc__:
            lines = [ln.strip() for ln in func.__doc__.strip().splitlines() if ln.strip()]
            resolved_description = lines[0] if lines else ""
        _ACTIONS[id] = ActionSpec(
            id=id,
            func=func,
            name=name or id,
            category=category,
            icon=icon,
            description=resolved_description,
            inputs=_as_fields(inputs),
            outputs=_as_fields(outputs),
            blocking=blocking,
            branches=branches,
        )
        return func

    return decorate


def trigger(
    id: str,
    name: str | None = None,
    *,
    category: str = "触发",
    icon: str | None = None,
    description: str = "",
    inputs: Mapping[str, Any] | None = None,
    outputs: Mapping[str, Any] | None = None,
    events: tuple[str, ...] = (),
    one_shot: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个函数注册成工作流触发器。"""

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        if id in _TRIGGERS:
            raise ValueError(f"触发器 id 重复：{id!r}")
        _TRIGGERS[id] = TriggerSpec(
            id=id,
            func=func,
            name=name or id,
            category=category,
            icon=icon,
            description=description,
            inputs=_as_fields(inputs),
            outputs=_as_fields(outputs),
            events=events,
            one_shot=one_shot,
        )
        return func

    return decorate


def registered_actions() -> dict[str, ActionSpec]:
    return dict(_ACTIONS)


def registered_triggers() -> dict[str, TriggerSpec]:
    return dict(_TRIGGERS)


def describe_interface() -> dict[str, Any]:
    """worker 响应内核 ``describe`` 时返回的内容。

    这就是画布左侧模块面板的全部数据来源。
    """
    return {
        "actions": {k: v.to_schema() for k, v in _ACTIONS.items()},
        "triggers": {k: v.to_schema() for k, v in _TRIGGERS.items()},
    }


def clear_registry() -> None:
    """只为测试用；正常运行时一个插件进程只加载一次。"""
    _ACTIONS.clear()
    _TRIGGERS.clear()
