"""界面元素插件：按定位器找元素并操作它。

这个插件本身很薄 —— 定位器的解析逻辑（五级降级）和图像匹配都在 SDK 里
（``myautowork.uia`` / ``myautowork.imaging``）。**这是故意的**：那套规则同时被
界面上的拾取器使用（反向：控件 → 定位器），两边共用一份实现才不会被时间拉开差距。

值得在这里说清楚的是**为什么 Element 参数值得单独一种类型**。它的值不是"一个字符串"
或"一个坐标"，而是一份**带降级方案的定位描述**。用户永远不该手写它 —— 手写
AutomationId 意味着要去翻 Inspect.exe，而 Qt 程序的 AutomationId 还是"应用名.窗口名.
控件名"拼出来的（实测），根本没法手写。所以它必须由拾取器产出。这也是为什么 SDK 里
``Element`` 字段天生带 ``picker``。
"""

from __future__ import annotations

import time
from typing import Any

import win32api
import win32con

from myautowork import (
    Bool,
    Context,
    Element,
    Enum,
    Integer,
    Locator,
    Number,
    String,
    action,
)
from myautowork import uia as bridge

__all__ = ["locate", "click", "read", "set_text", "wait", "focus"]


def _locator(value: Any) -> Locator:
    """参数值 -> Locator。对残缺数据宽容，让人能手工塞一个 {"name": "确定"} 先试。"""
    locator = Locator.from_dict(value)
    if locator.is_empty:
        raise ValueError("元素定位是空的 —— 先在属性面板上点 🎯 拾取一个")
    return locator


def _click_point(x: int, y: int, button: str = "left", double: bool = False) -> None:
    down, up = (
        (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP)
        if button == "left"
        else (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP)
    )
    win32api.SetCursorPos((int(x), int(y)))
    time.sleep(0.03)
    for index in range(2 if double else 1):
        win32api.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.01)
        win32api.mouse_event(up, 0, 0, 0, 0)
        if index == 0 and double:
            time.sleep(0.04)


def _report(ctx: Context, resolved: bridge.Resolved, locator: Locator) -> None:
    """把"用的是哪一级"报出来。

    这不是调试信息，是**安全提示**：如果一条流程靠坐标跑通了，用户有权知道自己在走钢丝 ——
    下次窗口挪一下它就会点到别的地方，而且不会报错。
    """
    ctx.info(f"命中元素：{resolved.describe()}（定位级别：{resolved.strategy}）")
    if locator.is_fragile:
        ctx.warning(
            "这条定位只靠图像/坐标，界面一挪就会失效 —— 建议在有 AutomationId 时重新拾取"
        )


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #

_ELEMENT_INPUT = Element(
    required=True,
    label="目标元素",
    help="点右边的 🎯 到屏幕上拾取；拾取器会把能找到的定位信息全记下来",
)


@action(
    id="locate",
    name="查找元素",
    category="界面",
    icon="crosshair",
    description="解析定位器并返回元素信息，不做任何操作。用来确认到底找没找到",
    inputs={
        "element": _ELEMENT_INPUT,
        "timeout": Number(default=5.0, label="最长等待（秒）"),
    },
    outputs={
        "found": Bool(label="是否找到"),
        "strategy": String(label="实际命中的定位级别"),
        "name": String(label="元素名称"),
        "control_type": String(label="控件类型"),
        "automation_id": String(label="AutomationId"),
        "rect": String(label="矩形 左,上,右,下"),
        "center": String(label="中心点 x,y"),
    },
)
def locate(
    ctx: Context, element: Any, timeout: float = 5.0, **_: Any
) -> dict[str, Any]:
    locator = _locator(element)
    resolved = bridge.resolve(locator, timeout=timeout, log=ctx.debug)
    _report(ctx, resolved, locator)
    return {
        "found": True,
        "strategy": resolved.strategy,
        "name": resolved.name,
        "control_type": resolved.control_type,
        "automation_id": resolved.automation_id,
        "rect": ",".join(str(v) for v in resolved.rect),
        "center": f"{resolved.center[0]},{resolved.center[1]}",
    }


@action(
    id="click",
    name="点击元素",
    category="界面",
    icon="mouse-pointer",
    description="找到元素并点击它的中心。窗口移动了也点得对，因为它每次都重新定位",
    inputs={
        "element": _ELEMENT_INPUT,
        "button": Enum(["left", "right"], default="left", label="按键"),
        "double": Bool(default=False, label="双击"),
        "timeout": Number(default=10.0, label="最长等待（秒）"),
        "offset_x": Integer(default=0, label="横向偏移", help="点元素中心偏左/右多少像素"),
        "offset_y": Integer(default=0, label="纵向偏移"),
    },
    outputs={
        "strategy": String(label="实际命中的定位级别"),
        "x": Integer(label="点击的 X"),
        "y": Integer(label="点击的 Y"),
        "name": String(label="元素名称"),
    },
)
def click(
    ctx: Context,
    element: Any,
    button: str = "left",
    double: bool = False,
    timeout: float = 10.0,
    offset_x: int = 0,
    offset_y: int = 0,
    **_: Any,
) -> dict[str, Any]:
    locator = _locator(element)
    resolved = bridge.resolve(locator, timeout=timeout, log=ctx.debug)
    _report(ctx, resolved, locator)

    center_x, center_y = resolved.center
    x = center_x + int(offset_x)
    y = center_y + int(offset_y)
    _click_point(x, y, button, double)
    ctx.info(f"{'双击' if double else '点击'} ({x}, {y})")
    return {"strategy": resolved.strategy, "x": x, "y": y, "name": resolved.name}


@action(
    id="read",
    name="读取文本",
    category="界面",
    icon="type",
    description="读出元素上的文本。优先取 Value（输入框里的内容），取不到再退回显示名称",
    inputs={
        "element": _ELEMENT_INPUT,
        "timeout": Number(default=5.0, label="最长等待（秒）"),
    },
    outputs={
        "text": String(label="文本"),
        "source": String(label="取自哪里（value / name）"),
        "strategy": String(label="实际命中的定位级别"),
    },
)
def read(ctx: Context, element: Any, timeout: float = 5.0, **_: Any) -> dict[str, Any]:
    locator = _locator(element)
    resolved = bridge.resolve(locator, timeout=timeout, log=ctx.debug)
    _report(ctx, resolved, locator)

    text = ""
    source = "name"
    if resolved.control is not None:
        try:
            pattern = resolved.control.GetValuePattern()
            value = pattern.Value
            if value:
                text, source = str(value), "value"
        except Exception:
            pass
    if not text:
        text = resolved.name

    preview = text if len(text) <= 40 else text[:40] + "…"
    ctx.info(f"读到（{source}）：{preview!r}")
    return {"text": text, "source": source, "strategy": resolved.strategy}


@action(
    id="set_text",
    name="写入文本",
    category="界面",
    icon="edit",
    description="往输入框里写文本。优先用 UIA 的 Value 接口（不抢焦点），失败则退回模拟输入",
    inputs={
        "element": _ELEMENT_INPUT,
        "text": String(required=True, label="文本"),
        "clear_first": Bool(default=True, label="先清空原有内容"),
        "timeout": Number(default=5.0, label="最长等待（秒）"),
    },
    outputs={
        "written": Bool(label="是否写入成功"),
        "method": String(label="用了哪种方式（value / paste）"),
        "strategy": String(label="实际命中的定位级别"),
    },
)
def set_text(
    ctx: Context,
    element: Any,
    text: str,
    clear_first: bool = True,
    timeout: float = 5.0,
    **_: Any,
) -> dict[str, Any]:
    locator = _locator(element)
    resolved = bridge.resolve(locator, timeout=timeout, log=ctx.debug)
    _report(ctx, resolved, locator)

    # 首选 UIA 的 Value 接口：它不经过键盘，所以不抢焦点、不受输入法影响。
    if resolved.control is not None:
        try:
            pattern = resolved.control.GetValuePattern()
            if pattern:
                pattern.SetValue(text)
                ctx.info("已通过 Value 接口写入（没有模拟键盘）")
                return {"written": True, "method": "value", "strategy": resolved.strategy}
        except Exception as exc:
            ctx.debug(f"Value 接口不可用（{type(exc).__name__}），退回模拟输入")

    # 退回：点进去、全选、打字。用剪贴板粘贴而不是逐字模拟，中文才可靠。
    centre_x, centre_y = resolved.center
    _click_point(centre_x, centre_y)
    time.sleep(0.05)
    if clear_first:
        for key, flag in ((win32con.VK_CONTROL, 0), (ord("A"), 0), (ord("A"), win32con.KEYEVENTF_KEYUP), (win32con.VK_CONTROL, win32con.KEYEVENTF_KEYUP)):
            win32api.keybd_event(key, 0, flag, 0)

    import win32clipboard  # noqa: PLC0415 - 只有走到这条退路才需要

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()

    time.sleep(0.03)
    for key, flag in ((win32con.VK_CONTROL, 0), (ord("V"), 0), (ord("V"), win32con.KEYEVENTF_KEYUP), (win32con.VK_CONTROL, win32con.KEYEVENTF_KEYUP)):
        win32api.keybd_event(key, 0, flag, 0)

    ctx.info(f"已通过剪贴板粘贴写入 {len(text)} 个字符")
    return {"written": True, "method": "paste", "strategy": resolved.strategy}


@action(
    id="wait",
    name="等待元素",
    category="界面",
    icon="clock",
    description="等元素出现（或等到它消失），用于等待上一步操作把界面打开",
    inputs={
        "element": _ELEMENT_INPUT,
        "timeout": Number(default=15.0, label="最长等待（秒）"),
        "until_gone": Bool(default=False, label="反过来等它消失"),
    },
    outputs={
        "found": Bool(label="是否等到了"),
        "strategy": String(label="实际命中的定位级别"),
        "waited": Number(label="实际等待秒数"),
    },
)
def wait(
    ctx: Context,
    element: Any,
    timeout: float = 15.0,
    until_gone: bool = False,
    **_: Any,
) -> dict[str, Any]:
    locator = _locator(element)
    started = time.monotonic()

    if until_gone:
        deadline = started + max(0.0, timeout)
        while True:
            try:
                bridge.resolve(locator, timeout=0.0, log=ctx.debug)
                present = True
            except bridge.ElementNotFound:
                present = False
            if not present:
                waited = round(time.monotonic() - started, 3)
                ctx.info(f"元素已消失，等了 {waited} 秒")
                return {"found": True, "strategy": "", "waited": waited}
            if time.monotonic() >= deadline:
                waited = round(time.monotonic() - started, 3)
                ctx.warning(f"等了 {waited} 秒，元素始终没消失")
                return {"found": False, "strategy": "", "waited": waited}
            time.sleep(0.25)

    try:
        resolved = bridge.resolve(locator, timeout=timeout, log=ctx.debug)
    except bridge.ElementNotFound as exc:
        waited = round(time.monotonic() - started, 3)
        ctx.warning(f"等了 {waited} 秒，元素没出现：{exc}")
        return {"found": False, "strategy": "", "waited": waited}

    waited = round(time.monotonic() - started, 3)
    _report(ctx, resolved, locator)
    ctx.info(f"元素已出现，等了 {waited} 秒")
    return {"found": True, "strategy": resolved.strategy, "waited": waited}


@action(
    id="focus",
    name="聚焦元素",
    category="界面",
    icon="target",
    description="把键盘焦点移到元素上，之后可以直接用键鼠插件打字",
    inputs={
        "element": _ELEMENT_INPUT,
        "timeout": Number(default=5.0, label="最长等待（秒）"),
    },
    outputs={
        "focused": Bool(label="是否成功"),
        "strategy": String(label="实际命中的定位级别"),
    },
)
def focus(ctx: Context, element: Any, timeout: float = 5.0, **_: Any) -> dict[str, Any]:
    locator = _locator(element)
    resolved = bridge.resolve(locator, timeout=timeout, log=ctx.debug)
    _report(ctx, resolved, locator)

    if resolved.control is not None:
        try:
            resolved.control.SetFocus()
            ctx.info("已聚焦")
            return {"focused": True, "strategy": resolved.strategy}
        except Exception as exc:
            ctx.warning(f"UIA 聚焦失败（{type(exc).__name__}），改用点击")

    _click_point(*resolved.center)
    ctx.info("已通过点击聚焦")
    return {"focused": True, "strategy": resolved.strategy}
