"""键盘鼠标插件。

**文本输入为什么走 Unicode 注入而不是模拟按键。** 模拟按键的路线是"按下哪个键"，
要经过键盘布局翻译 —— 想把"中文"两个字打进去，就得知道当前输入法、得先切输入法、
还得等候选词。而 ``KEYEVENTF_UNICODE`` 是直接把字符交给目标窗口（等价于输入法已经
上屏的结果），跟布局无关，中英文一视同仁。这是唯一能可靠输入非 ASCII 的办法。

**为什么不用 pyautogui / pydirectinput。** 它们能用，但对本项目是两个多余的东西：
pyautogui 带 FAILSAFE（鼠标碰到屏幕角落就抛异常，在自动化里是纯粹的坑），而
pydirectinput 是为游戏 DirectInput 设计的，对普通窗口反而绕远路。这里直接用
``SendInput``，行为完全可控，也少两个依赖。

**点击为什么不走 UIA。** 这个插件是"底层手"：只知道屏幕坐标。要"点某个控件"请用
``win.uia`` 的点击动作 —— 它先解析元素再算中心点，窗口一动依然正确。这里保留坐标点击
是因为总有些东西 UIA 看不见（游戏、Canvas 绘制、自绘界面），那时坐标是唯一的路。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any

import win32api
import win32con
import win32gui

from myautowork import Bool, Context, Enum, Integer, Number, String, Text, action

__all__ = ["click", "type_text", "press_keys", "cursor", "scroll", "drag"]

_user32 = ctypes.WinDLL("user32", use_last_error=True)

# -- SendInput 结构体 -------------------------------------------------------- #
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
_user32.SendInput.restype = wintypes.UINT


def _send(*inputs: _INPUT) -> None:
    array = (_INPUT * len(inputs))(*inputs)
    sent = _user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise RuntimeError(f"SendInput 只发出了 {sent}/{len(inputs)} 个事件（可能被 UIPI 拦了）")


def _key_event(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    item = _INPUT()
    item.type = INPUT_KEYBOARD
    item.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return item


# -- 按键解析 ---------------------------------------------------------------- #

_NAMED_KEYS: dict[str, int] = {
    "ctrl": win32con.VK_CONTROL,
    "control": win32con.VK_CONTROL,
    "alt": win32con.VK_MENU,
    "shift": win32con.VK_SHIFT,
    "win": win32con.VK_LWIN,
    "enter": win32con.VK_RETURN,
    "return": win32con.VK_RETURN,
    "tab": win32con.VK_TAB,
    "esc": win32con.VK_ESCAPE,
    "escape": win32con.VK_ESCAPE,
    "space": win32con.VK_SPACE,
    "backspace": win32con.VK_BACK,
    "delete": win32con.VK_DELETE,
    "del": win32con.VK_DELETE,
    "insert": win32con.VK_INSERT,
    "home": win32con.VK_HOME,
    "end": win32con.VK_END,
    "pageup": win32con.VK_PRIOR,
    "pagedown": win32con.VK_NEXT,
    "up": win32con.VK_UP,
    "down": win32con.VK_DOWN,
    "left": win32con.VK_LEFT,
    "right": win32con.VK_RIGHT,
    "capslock": win32con.VK_CAPITAL,
    "printscreen": win32con.VK_SNAPSHOT,
}
for _index in range(1, 25):
    _NAMED_KEYS[f"f{_index}"] = win32con.VK_F1 + _index - 1


def _parse_keys(combo: str) -> list[int]:
    """``"ctrl+shift+s"`` -> ``[0x11, 0x10, 0x53]``。"""
    keys: list[int] = []
    for raw in combo.replace(" ", "").split("+"):
        if not raw:
            continue
        token = raw.lower()
        if token in _NAMED_KEYS:
            keys.append(_NAMED_KEYS[token])
        elif len(token) == 1:
            keys.append(ord(token.upper()))
        else:
            raise ValueError(f"不认识这个键：{raw!r}（可用：单字符、F1-F24、或 ctrl/alt/shift/tab/enter…）")
    if not keys:
        raise ValueError("按键组合是空的")
    return keys


# -- 动作 -------------------------------------------------------------------- #

_BUTTON_FLAGS = {
    "left": (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP),
    "right": (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP),
    "middle": (win32con.MOUSEEVENTF_MIDDLEDOWN, win32con.MOUSEEVENTF_MIDDLEUP),
}


@action(
    id="click",
    name="点击坐标",
    category="界面",
    icon="mouse-pointer",
    description="把鼠标移到屏幕坐标并点击。窗口会动的话请改用 win.uia 的点击",
    inputs={
        "x": Integer(required=True, label="X"),
        "y": Integer(required=True, label="Y"),
        "button": Enum(["left", "right", "middle"], default="left", label="按键"),
        "count": Integer(default=1, label="点击次数", help="2 就是双击"),
        "interval": Number(default=0.08, label="多次点击间隔（秒）"),
        "move_first": Bool(default=True, label="先移动鼠标过去"),
    },
    outputs={
        "x": Integer(label="实际点击的 X"),
        "y": Integer(label="实际点击的 Y"),
        "count": Integer(label="点击次数"),
    },
)
def click(
    ctx: Context,
    x: int,
    y: int,
    button: str = "left",
    count: int = 1,
    interval: float = 0.08,
    move_first: bool = True,
    **_: Any,
) -> dict[str, Any]:
    if button not in _BUTTON_FLAGS:
        raise ValueError(f"不支持的按键：{button!r}")
    down, up = _BUTTON_FLAGS[button]

    if move_first:
        win32api.SetCursorPos((int(x), int(y)))
        time.sleep(0.02)

    times = max(1, int(count))
    for index in range(times):
        win32api.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.01)
        win32api.mouse_event(up, 0, 0, 0, 0)
        if index < times - 1:
            time.sleep(max(0.0, interval))

    ctx.info(f"在 ({x}, {y}) {'双击' if times == 2 else f'点击 {times} 次'}（{button}）")
    return {"x": int(x), "y": int(y), "count": times}


@action(
    id="type_text",
    name="输入文本",
    category="界面",
    icon="keyboard",
    description="逐字输入文本。走 Unicode 注入，中文、emoji 都能直接打进去",
    inputs={
        "text": Text(required=True, label="要输入的文本"),
        "interval": Number(default=0.012, label="每字间隔（秒）", help="界面慢的话调大一点"),
    },
    outputs={
        "length": Integer(label="字符数"),
        "elapsed": Number(label="实际耗时（秒）"),
    },
)
def type_text(ctx: Context, text: str, interval: float = 0.012, **_: Any) -> dict[str, Any]:
    if not text:
        return {"length": 0, "elapsed": 0.0}

    started = time.monotonic()
    delay = max(0.0, float(interval))
    for char in text:
        code = ord(char)
        if code > 0xFFFF:
            # 星光平面字符（emoji 等）要拆成代理对，跟 Windows 内部表示一致。
            code -= 0x10000
            units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
        else:
            units = [code]

        if char == "\n":
            _send(_key_event(vk=win32con.VK_RETURN), _key_event(vk=win32con.VK_RETURN, flags=KEYEVENTF_KEYUP))
        elif char == "\t":
            _send(_key_event(vk=win32con.VK_TAB), _key_event(vk=win32con.VK_TAB, flags=KEYEVENTF_KEYUP))
        else:
            events: list[_INPUT] = []
            for unit in units:
                events.append(_key_event(scan=unit, flags=KEYEVENTF_UNICODE))
                events.append(_key_event(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
            _send(*events)

        if delay:
            time.sleep(delay)

    elapsed = round(time.monotonic() - started, 3)
    preview = text if len(text) <= 24 else text[:24] + "…"
    ctx.info(f"已输入 {len(text)} 个字符：{preview!r}（{elapsed} 秒）")
    return {"length": len(text), "elapsed": elapsed}


@action(
    id="press_keys",
    name="按键",
    category="界面",
    icon="command",
    description="按一个组合键，如 ctrl+c、alt+f4、enter",
    inputs={
        "keys": String(
            required=True,
            label="按键组合",
            placeholder="ctrl+shift+s",
            help="用 + 连接。可用：单字符、F1-F24、ctrl/alt/shift/win/tab/enter/esc/方向键…",
        ),
        "repeat": Integer(default=1, label="重复次数"),
        "interval": Number(default=0.05, label="重复间隔（秒）"),
    },
    outputs={
        "keys": String(label="实际按下的组合"),
        "repeat": Integer(label="重复次数"),
    },
)
def press_keys(
    ctx: Context, keys: str, repeat: int = 1, interval: float = 0.05, **_: Any
) -> dict[str, Any]:
    parsed = _parse_keys(keys)
    times = max(1, int(repeat))

    for index in range(times):
        events = [_key_event(vk=vk) for vk in parsed]
        events += [_key_event(vk=vk, flags=KEYEVENTF_KEYUP) for vk in reversed(parsed)]
        _send(*events)
        if index < times - 1:
            time.sleep(max(0.0, interval))

    ctx.info(f"已按下 {keys} × {times}")
    return {"keys": keys, "repeat": times}


@action(
    id="cursor",
    name="鼠标位置",
    category="界面",
    icon="crosshair",
    description="读取鼠标当前坐标，用来确认该点哪里",
    inputs={
        "delay": Number(default=0.0, label="先等待几秒（秒）", help="方便你把鼠标移到位再读"),
    },
    outputs={
        "x": Integer(label="X"),
        "y": Integer(label="Y"),
        "window_title": String(label="该位置所属窗口的标题"),
    },
)
def cursor(ctx: Context, delay: float = 0.0, **_: Any) -> dict[str, Any]:
    if delay > 0:
        ctx.info(f"等待 {delay} 秒，请把鼠标移到目标位置")
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            time.sleep(0.05)

    x, y = win32gui.GetCursorPos()
    # WindowFromPoint 拿到的是最上层窗口；RootWindow 走到顶层，标题才有意义。
    handle = win32gui.WindowFromPoint((x, y))
    root = win32gui.GetAncestor(handle, win32con.GA_ROOT) if handle else 0
    title = win32gui.GetWindowText(root) if root else ""
    ctx.info(f"鼠标在 ({x}, {y})" + (f"，窗口：{title}" if title else ""))
    return {"x": int(x), "y": int(y), "window_title": title}


@action(
    id="scroll",
    name="滚动",
    category="界面",
    icon="mouse",
    description="在指定位置滚动滚轮",
    inputs={
        "x": Integer(default=0, label="X", help="留 0 表示在鼠标当前位置滚动"),
        "y": Integer(default=0, label="Y"),
        "amount": Integer(required=True, label="滚动量", help="正数向上、负数向下，一格是 120"),
    },
    outputs={"amount": Integer(label="实际滚动量")},
)
def scroll(ctx: Context, amount: int, x: int = 0, y: int = 0, **_: Any) -> dict[str, Any]:
    if x or y:
        win32api.SetCursorPos((int(x), int(y)))
        time.sleep(0.02)
    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, int(amount), 0)
    ctx.info(f"滚轮 {amount}")
    return {"amount": int(amount)}


@action(
    id="drag",
    name="拖拽",
    category="界面",
    icon="move",
    description="从一点按住拖到另一点，中间可以分步移动（有些界面需要中间帧才认）",
    inputs={
        "x1": Integer(required=True, label="起点 X"),
        "y1": Integer(required=True, label="起点 Y"),
        "x2": Integer(required=True, label="终点 X"),
        "y2": Integer(required=True, label="终点 Y"),
        "steps": Integer(default=12, label="中间步数"),
        "duration": Number(default=0.35, label="整个过程时长（秒）"),
    },
    outputs={
        "from": String(label="起点"),
        "to": String(label="终点"),
    },
)
def drag(
    ctx: Context,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    steps: int = 12,
    duration: float = 0.35,
    **_: Any,
) -> dict[str, Any]:
    count = max(1, int(steps))
    pause = max(0.0, duration) / count

    win32api.SetCursorPos((int(x1), int(y1)))
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)

    for index in range(1, count + 1):
        ratio = index / count
        win32api.SetCursorPos(
            (int(x1 + (x2 - x1) * ratio), int(y1 + (y2 - y1) * ratio))
        )
        time.sleep(pause)

    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    ctx.info(f"已从 ({x1}, {y1}) 拖到 ({x2}, {y2})")
    return {"from": f"{x1},{y1}", "to": f"{x2},{y2}"}
