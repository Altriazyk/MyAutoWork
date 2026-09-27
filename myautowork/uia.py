"""Windows UI Automation 互操作层。

**为什么这一层在 SDK 里，而不是塞进 win.uia 插件。** 因为它有两个消费者，而且需求相反：

- **运行时**（``win.uia`` 插件）：把一份定位器**解析成**真实控件，然后操作它。
- **设计时**（界面上的拾取器）：把光标下的控件**反解成**一份定位器，而且要跟得上鼠标
  移动的速度。

两边必须用同一套规则 —— 拾取器写出来的路径格式，解析器得认。这份规则放一个地方，比在
两个进程里各维护一份实现可靠得多（何况它们一定会漂移）。所以 SDK 提供两件事：
``resolve()`` 和 ``locator_from_control()``，互为逆运算。

``uiautomation`` 是**延迟导入**的：不做界面自动化的插件不该被这个依赖拖累
（``import myautowork`` 不会碰它）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable

from .locator import Locator, PathStep

__all__ = [
    "ElementNotFound",
    "Resolved",
    "UiaUnavailable",
    "auto",
    "control_at",
    "control_info",
    "locator_from_control",
    "resolve",
    "window_root",
]

#: 拾取器只想知道"光标下是什么"时，往窗口上走几级就够了 —— 走太深会拖慢每一次移动。
_DEFAULT_ANCESTOR_LIMIT = 12


class UiaUnavailable(RuntimeError):
    """没装 uiautomation，或者系统没开 UIA。"""


class ElementNotFound(RuntimeError):
    """定位器里的每一级都没命中。"""


def auto() -> Any:
    """拿到 ``uiautomation`` 模块。没装的话给一句人话，而不是 ImportError 栈。"""
    try:
        import uiautomation  # noqa: PLC0415 - 故意延迟导入
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise UiaUnavailable(
            "界面自动化需要 uiautomation：pip install uiautomation"
        ) from exc
    return uiautomation


@dataclass
class Resolved:
    """解析结果。可能是真实控件，也可能只是屏幕上的一个点。"""

    strategy: str
    point: tuple[int, int] = (0, 0)
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    name: str = ""
    control_type: str = ""
    automation_id: str = ""
    class_name: str = ""
    control: Any = None
    notes: list[str] = dc_field(default_factory=list)

    @property
    def is_uia(self) -> bool:
        return self.control is not None

    @property
    def center(self) -> tuple[int, int]:
        if self.point != (0, 0):
            return self.point
        left, top, right, bottom = self.rect
        return ((left + right) // 2, (top + bottom) // 2)

    def describe(self) -> str:
        kind = self.control_type.replace("Control", "") if self.control_type else "坐标"
        label = self.name or self.automation_id or self.class_name
        where = f"({self.center[0]},{self.center[1]})"
        return f"{kind} {label}".strip() + f" {where}"


# --------------------------------------------------------------------------- #
# 反解：控件 -> 定位器（拾取器用）
# --------------------------------------------------------------------------- #


def _safe(getter: Callable[[], Any], default: Any = "") -> Any:
    """UIA 属性读取随时可能抛（元素在读取的瞬间被销毁）。读不到就用默认值。"""
    try:
        value = getter()
    except Exception:
        return default
    return default if value is None else value


def control_info(control: Any) -> dict[str, Any]:
    """把控件的关键属性抓成一个字典。拾取器拿它做预览。"""
    rect = _safe(lambda: control.BoundingRectangle, None)
    box = (0, 0, 0, 0)
    if rect is not None:
        box = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    return {
        "name": str(_safe(lambda: control.Name, "")),
        "automation_id": str(_safe(lambda: control.AutomationId, "")),
        "control_type": str(_safe(lambda: control.ControlTypeName, "")),
        "class_name": str(_safe(lambda: control.ClassName, "")),
        "rect": box,
        "enabled": bool(_safe(lambda: control.IsEnabled, True)),
    }


def _window_of(control: Any) -> Any:
    """往上找到所属窗口。"""
    module = auto()
    current = control
    for _ in range(_DEFAULT_ANCESTOR_LIMIT):
        if current is None:
            break
        if current.ControlTypeName in ("WindowControl", "PaneControl"):
            return current
        current = _safe(lambda c=current: c.GetParentControl(), None)
        if current is None:
            break
    return _safe(lambda: module.GetRootControl(), None)


def _ancestors(control: Any, limit: int = _DEFAULT_ANCESTOR_LIMIT) -> list[Any]:
    """从控件往上走到窗口（含两端），返回 从窗口到控件 的顺序。"""
    chain: list[Any] = []
    current = control
    for _ in range(limit):
        if current is None:
            break
        chain.append(current)
        if current.ControlTypeName in ("WindowControl", "PaneControl"):
            break
        current = _safe(lambda c=current: c.GetParentControl(), None)
    chain.reverse()
    return chain


def _same_control(a: Any, b: Any) -> bool:
    """两个 UIA 包装对象指向的是不是同一个控件。

    **不能用 ``is``。** 每次 ``GetChildren()`` 都返回新的 Python 包装对象，包着同一个
    COM 控件，但 ``is`` 永远为 False。实测踩过：窗口里只有一个叫「确定」的按钮，序号
    却算成了 1，于是路径定位永远差一格。RuntimeId 是 UIA 给元素的稳定标识，用它比。
    """
    if a is None or b is None:
        return False
    if a is b:
        return True
    try:
        return tuple(a.GetRuntimeId()) == tuple(b.GetRuntimeId())
    except Exception:
        pass
    try:
        ra, rb = a.BoundingRectangle, b.BoundingRectangle
        return (ra.left, ra.top, ra.right, ra.bottom) == (rb.left, rb.top, rb.right, rb.bottom)
    except Exception:
        return False


def _step_for(control: Any, parent: Any) -> PathStep:
    """算出这一级在父节点里的序号。

    同名同类型的兄弟很常见（一排按钮、一列表格行），所以序号是必需的 —— 不然路径
    永远只会命中第一个。
    """
    control_type = str(_safe(lambda: control.ControlTypeName, ""))
    name = str(_safe(lambda: control.Name, ""))
    index = 0
    if parent is not None:
        siblings = _safe(lambda: parent.GetChildren(), []) or []
        seen = 0
        for sibling in siblings:
            if _same_control(sibling, control):
                break
            if (
                str(_safe(lambda s=sibling: s.ControlTypeName, "")) == control_type
                and str(_safe(lambda s=sibling: s.Name, "")) == name
            ):
                seen += 1
        index = seen
    return PathStep(control_type=control_type, name=name, index=index)


def locator_from_control(
    control: Any, with_path: bool = True, screen_size: tuple[int, int] | None = None
) -> Locator:
    """把一个控件反解成定位器。

    ``with_path=False`` 时不算父子链 —— 拾取器在鼠标移动过程中每秒要调很多次，
    算路径是这里唯一有点开销的部分，留到真正采集的时候再算。
    """
    if control is None:
        return Locator()

    info = control_info(control)
    left, top, right, bottom = info["rect"]
    window = _window_of(control)
    window_info = control_info(window) if window is not None else {}

    path: tuple[PathStep, ...] = ()
    if with_path:
        chain = _ancestors(control)
        steps: list[PathStep] = []
        for position, node in enumerate(chain):
            parent = chain[position - 1] if position else None
            steps.append(_step_for(node, parent))
        path = tuple(steps)

    return Locator(
        automation_id=info["automation_id"],
        name=info["name"],
        control_type=info["control_type"],
        class_name=info["class_name"],
        window_title=str(window_info.get("name") or ""),
        window_class=str(window_info.get("class_name") or ""),
        path=path,
        rect=(left, top, right, bottom),
        screen=tuple(screen_size) if screen_size else (0, 0),
        point=((left + right) // 2, (top + bottom) // 2),
    )


def control_at(x: int, y: int) -> Any:
    """光标下最上层的控件。拾取器的高频调用入口。"""
    module = auto()
    try:
        return module.ControlFromPoint(int(x), int(y))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 解析：定位器 -> 控件（运行时用）
# --------------------------------------------------------------------------- #


def window_root(locator: Locator) -> Any:
    """定位的起点永远是窗口，不是整个桌面 —— 不然"确定"按钮会在全桌面里撞名。"""
    module = auto()
    if not locator.window_title and not locator.window_class:
        return module.GetRootControl()

    import re  # noqa: PLC0415 - 只在这里用

    conditions: dict[str, Any] = {}
    if locator.window_title:
        conditions["RegexName"] = ".*" + re.escape(locator.window_title) + ".*"
    if locator.window_class:
        conditions["ClassName"] = locator.window_class

    for factory in (module.WindowControl, module.PaneControl):
        try:
            control = factory(searchDepth=1, **conditions)
        except Exception:
            continue
        if control is not None and _safe(lambda c=control: c.Exists(0, 0), False):
            return control
    return None


def _by_automation_id(root: Any, locator: Locator) -> Any:
    return root.Control(searchDepth=0xFFFFFFFF, AutomationId=locator.automation_id)


def _by_name_type(root: Any, locator: Locator) -> Any:
    kwargs: dict[str, Any] = {"searchDepth": 0xFFFFFFFF, "Name": locator.name}
    control_type_id = getattr(auto().ControlType, locator.control_type, None)
    if control_type_id is None:
        return None
    kwargs["ControlType"] = control_type_id
    return root.Control(**kwargs)


def _child_matching(parent: Any, step: PathStep) -> Any:
    children = _safe(lambda: parent.GetChildren(), []) or []
    seen = 0
    for child in children:
        if str(_safe(lambda c=child: c.ControlTypeName, "")) != step.control_type:
            continue
        if step.name and str(_safe(lambda c=child: c.Name, "")) != step.name:
            continue
        if seen == step.index:
            return child
        seen += 1
    return None


def _by_path(root: Any, locator: Locator) -> Any:
    current = root
    for step in locator.path:
        if current is None:
            return None
        # 路径的第一级通常就是窗口本身 —— 起点已经是它了，跳过。
        if (
            step.control_type == str(_safe(lambda: current.ControlTypeName, ""))
            and (not step.name or step.name == str(_safe(lambda: current.Name, "")))
        ):
            continue
        current = _child_matching(current, step)
    return current


def _existed(control: Any) -> bool:
    if control is None:
        return False
    return bool(_safe(lambda: control.Exists(0, 0), False))


def _try_strategy(locator: Locator, strategy: str, notes: list[str]) -> Resolved | None:
    if strategy == "point":
        x, y = locator.point
        return Resolved(strategy="point", point=(int(x), int(y)), notes=notes)

    if strategy == "image":
        from . import imaging  # noqa: PLC0415 - 只有走到这一级才需要 Pillow

        # 注意不能写 ``locator.screen or None``：(0, 0) 是非空元组，永远为真，
        # 于是"没有屏幕尺寸提示"会被当成"屏幕是 0×0"，匹配直接判定环境变了而放弃。
        screen_hint = tuple(locator.screen) if tuple(locator.screen) != (0, 0) else None
        hit = imaging.find(locator.image, locator.rect, screen_size=screen_hint)
        if hit is None:
            return None
        x, y, score = hit
        notes.append(f"图像匹配得分 {score}")
        return Resolved(strategy="image", point=(x, y), notes=notes)

    root = window_root(locator)
    if root is None:
        notes.append(f"找不到窗口 {locator.window_title or locator.window_class!r}")
        return None

    if strategy == "automation_id":
        control = _by_automation_id(root, locator)
    elif strategy == "name_type":
        control = _by_name_type(root, locator)
    elif strategy == "path":
        control = _by_path(root, locator)
    else:  # pragma: no cover - STRATEGY_ORDER 已经穷举
        return None

    if not _existed(control):
        return None

    info = control_info(control)
    return Resolved(
        strategy=strategy,
        point=((info["rect"][0] + info["rect"][2]) // 2, (info["rect"][1] + info["rect"][3]) // 2),
        rect=info["rect"],
        name=info["name"],
        control_type=info["control_type"],
        automation_id=info["automation_id"],
        class_name=info["class_name"],
        control=control,
        notes=notes,
    )


def resolve(
    locator: Locator,
    timeout: float = 5.0,
    poll: float = 0.3,
    log: Callable[[str], None] | None = None,
) -> Resolved:
    """按降级链解析，返回第一个命中的结果。

    **整条链会在超时时间内反复重试**，而不是"试一遍就报找不到" —— 界面自动化的场景里，
    元素往往是被上一步操作叫出来的，差个几百毫秒很正常。
    """
    strategies = locator.available()
    if not strategies:
        raise ElementNotFound("定位器是空的 —— 先在属性面板上拾取一个元素")

    deadline = time.monotonic() + max(0.0, timeout)
    notes: list[str] = []
    round_number = 0

    while True:
        round_number += 1
        for strategy in strategies:
            local_notes: list[str] = []
            try:
                found = _try_strategy(locator, strategy, local_notes)
            except UiaUnavailable:
                raise
            except Exception as exc:
                local_notes.append(f"{strategy} 出错：{type(exc).__name__}: {exc}")
                found = None

            if found is not None:
                if log is not None:
                    note = "；".join(found.notes) if found.notes else ""
                    log(f"命中 {strategy}（第 {round_number} 轮）{note}")
                return found
            notes.extend(local_notes)

        if time.monotonic() >= deadline:
            break
        time.sleep(poll)

    detail = "；".join(notes[-6:]) if notes else "所有级别都没有命中"
    raise ElementNotFound(
        f"找不到元素（试过 {locator.describe_strategies()}，等了 {timeout} 秒）：{detail}"
    )
