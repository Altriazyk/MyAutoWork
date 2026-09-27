"""窗口操作插件。

界面自动化的第一步永远是"**先把目标窗口弄到前台**"。不激活就点，点到的是你原来那个
窗口 —— 这是最常见的自动化事故，而且现场看起来像"坐标算错了"，很难查。所以
``activate`` 的返回里带了窗口矩形：下一步多半要在这块区域里点某个位置，或者在里面找元素。

关于 SetForegroundWindow 为什么需要那么长的兜底：Windows 有一条"前台锁"规则 —— 只有
当前拥有前台的线程才能把前台交给别人。我们的插件进程通常不是，于是调用会静默失败
（不抛异常，只是没生效）。办法是先把自己的输入队列挂到当前前台线程上，挂上之后系统就
把我们当成它的一部分，这时再设置前台就会被接受。这就是那段 AttachThreadInput 的来历，
不是防御性编程，是必需的。
"""

from __future__ import annotations

import time
from typing import Any

import win32api
import win32con
import win32gui
import win32process

from myautowork import Bool, Context, Integer, List_, Number, String, action

__all__ = ["activate", "wait", "list_windows", "close", "move", "info"]


# --------------------------------------------------------------------------- #
# 窗口枚举与匹配
# --------------------------------------------------------------------------- #


def _enum_windows() -> list[dict[str, Any]]:
    """所有**可见且有标题**的顶层窗口。

    过滤掉没标题的：桌面、托盘、一堆 `Windows.UI.Core.CoreWindow` 之类的系统窗口都会
    混进来，让"按标题找窗口"这件事变得没法用。
    """
    found: list[dict[str, Any]] = []

    def callback(hwnd: int, _param: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return True
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:  # pragma: no cover - 窗口可能在枚举途中消失
            return True
        found.append(
            {
                "handle": int(hwnd),
                "title": title,
                "class_name": win32gui.GetClassName(hwnd),
                "rect": [int(left), int(top), int(right), int(bottom)],
            }
        )
        return True

    win32gui.EnumWindows(callback, None)
    return found


def _matches(
    windows: list[dict[str, Any]], title: str, window_class: str, exact: bool
) -> list[dict[str, Any]]:
    wanted = title.strip()
    wanted_class = window_class.strip().lower()
    result = []
    for window in windows:
        if wanted:
            if exact:
                if window["title"] != wanted:
                    continue
            elif wanted.lower() not in window["title"].lower():
                continue
        if wanted_class and wanted_class not in window["class_name"].lower():
            continue
        result.append(window)
    return result


def _find(
    title: str, window_class: str = "", exact: bool = False, timeout: float = 0.0
) -> dict[str, Any] | None:
    """找窗口，找不到就在 ``timeout`` 秒内反复找。

    默认不是"找一次就放弃"：界面自动化的场景里，窗口往往是被上一步操作叫出来的，
    差个几百毫秒很正常。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        found = _matches(_enum_windows(), title, window_class, exact)
        if found:
            return found[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.15)


def _bring_to_front(hwnd: int) -> bool:
    """把窗口弄到前台。返回是否成功。"""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    except Exception:  # pragma: no cover - 窗口已销毁
        return False

    try:
        win32gui.SetForegroundWindow(hwnd)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    except Exception:
        pass

    # 前台锁：先挂到前台线程的输入队列上，再设置前台。
    foreground = win32gui.GetForegroundWindow()
    current = win32api.GetCurrentThreadId()
    attached: list[int] = []
    try:
        try:
            win32gui.AllowSetForegroundWindow(win32con.ASFW_ANY)
        except Exception:
            pass

        for candidate in (
            win32process.GetWindowThreadProcessId(foreground)[0] if foreground else 0,
            win32process.GetWindowThreadProcessId(hwnd)[0],
        ):
            if candidate and candidate != current and candidate not in attached:
                win32process.AttachThreadInput(candidate, current, True)
                attached.append(candidate)

        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False
    finally:
        for thread_id in attached:
            try:
                win32process.AttachThreadInput(thread_id, current, False)
            except Exception:
                pass


def _describe(window: dict[str, Any]) -> str:
    left, top, right, bottom = window["rect"]
    return (
        f"{window['title']}  [{window['class_name']}]  "
        f"{right - left}×{bottom - top} @ ({left},{top})  hwnd={window['handle']}"
    )


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #


@action(
    id="activate",
    name="激活窗口",
    category="界面",
    icon="window",
    description="按标题找到窗口并调到前台；找不到可以在超时时间内一直等",
    inputs={
        "title": String(
            required=True,
            label="窗口标题",
            placeholder="记事本",
            help="默认按「包含」匹配，写一部分就够",
        ),
        "window_class": String(
            default="", label="窗口类名", help="可选。标题会变但类名不会，如 Notepad"
        ),
        "exact": Bool(default=False, label="标题精确匹配"),
        "timeout": Number(default=10.0, label="最长等待（秒）", help="0 表示只找一次"),
    },
    outputs={
        "handle": Integer(label="窗口句柄"),
        "title": String(label="匹配到的标题"),
        "class_name": String(label="窗口类名"),
        "rect": List_("integer", label="窗口矩形 [左, 上, 右, 下]"),
        "center": List_("integer", label="窗口中心点 [x, y]"),
    },
)
def activate(
    ctx: Context,
    title: str,
    window_class: str = "",
    exact: bool = False,
    timeout: float = 10.0,
    **_: Any,
) -> dict[str, Any]:
    window = _find(title, window_class, exact, timeout)
    if window is None:
        raise RuntimeError(
            f"没找到窗口：标题包含 {title!r}"
            + (f"，类名包含 {window_class!r}" if window_class else "")
            + f"（等了 {timeout} 秒）"
        )

    ctx.info(f"找到窗口：{_describe(window)}")
    if not _bring_to_front(window["handle"]):
        ctx.warning("窗口已找到，但没能调到前台 —— 后续点击可能落在别的窗口上")
    else:
        ctx.info("已激活")

    left, top, right, bottom = window["rect"]
    return {
        "handle": window["handle"],
        "title": window["title"],
        "class_name": window["class_name"],
        "rect": [left, top, right, bottom],
        "center": [(left + right) // 2, (top + bottom) // 2],
    }


@action(
    id="wait",
    name="等待窗口",
    category="界面",
    icon="clock",
    description="等某个窗口出现（或等到它消失），用于等待上一步操作把界面打开",
    inputs={
        "title": String(required=True, label="窗口标题", help="按包含匹配"),
        "window_class": String(default="", label="窗口类名"),
        "exact": Bool(default=False, label="标题精确匹配"),
        "timeout": Number(default=30.0, label="最长等待（秒）"),
        "until_gone": Bool(default=False, label="反过来等它消失"),
    },
    outputs={
        "found": Bool(label="是否等到了"),
        "waited": Number(label="实际等待秒数"),
        "handle": Integer(label="窗口句柄（等消失时为 0）"),
        "title": String(label="匹配到的标题"),
    },
)
def wait(
    ctx: Context,
    title: str,
    window_class: str = "",
    exact: bool = False,
    timeout: float = 30.0,
    until_gone: bool = False,
    **_: Any,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    while True:
        window = _find(title, window_class, exact, 0.0)
        hit = (window is None) if until_gone else (window is not None)
        if hit:
            waited = round(time.monotonic() - started, 3)
            if until_gone:
                ctx.info(f"窗口已消失，等了 {waited} 秒")
            else:
                ctx.info(f"窗口已出现（{waited} 秒）：{_describe(window)}")
            return {
                "found": True,
                "waited": waited,
                "handle": 0 if until_gone else window["handle"],
                "title": "" if until_gone else window["title"],
            }
        if time.monotonic() >= deadline:
            waited = round(time.monotonic() - started, 3)
            action_text = "消失" if until_gone else "出现"
            ctx.warning(f"等了 {waited} 秒，窗口始终没有{action_text}")
            return {"found": False, "waited": waited, "handle": 0, "title": ""}
        time.sleep(0.15)


@action(
    id="list",
    name="列出窗口",
    category="界面",
    icon="list",
    description="列出当前所有可见窗口，常用来确认该匹配哪个标题",
    inputs={
        "filter": String(default="", label="标题过滤", help="留空则列出全部"),
    },
    outputs={
        "count": Integer(label="数量"),
        "titles": List_("string", label="标题列表"),
        "lines": List_("string", label="详细列表（每行一个窗口）"),
    },
)
def list_windows(ctx: Context, filter: str = "", **_: Any) -> dict[str, Any]:
    windows = _matches(_enum_windows(), filter, "", False)
    windows.sort(key=lambda w: w["title"].lower())

    lines = [_describe(window) for window in windows]
    if not windows:
        ctx.warning("没有匹配的可见窗口")
    else:
        ctx.info(f"共 {len(windows)} 个窗口")
        for line in lines:
            ctx.debug(f"  {line}")

    return {
        "count": len(windows),
        "titles": [window["title"] for window in windows],
        "lines": lines,
    }


@action(
    id="close",
    name="关闭窗口",
    category="界面",
    icon="x",
    description="向窗口发关闭消息（等同于点右上角 ×，程序可能弹「是否保存」）",
    inputs={
        "title": String(required=True, label="窗口标题"),
        "window_class": String(default="", label="窗口类名"),
        "exact": Bool(default=False, label="标题精确匹配"),
        "timeout": Number(default=5.0, label="查找窗口的等待（秒）"),
    },
    outputs={
        "closed": Bool(label="是否发出了关闭消息"),
        "title": String(label="被关闭的标题"),
    },
)
def close(
    ctx: Context,
    title: str,
    window_class: str = "",
    exact: bool = False,
    timeout: float = 5.0,
    **_: Any,
) -> dict[str, Any]:
    window = _find(title, window_class, exact, timeout)
    if window is None:
        ctx.warning(f"没找到要关闭的窗口：{title!r}")
        return {"closed": False, "title": ""}

    win32gui.PostMessage(window["handle"], win32con.WM_CLOSE, 0, 0)
    ctx.info(f"已请求关闭：{window['title']}")
    return {"closed": True, "title": window["title"]}


@action(
    id="move",
    name="移动窗口",
    category="界面",
    icon="move",
    description="移动窗口到指定位置，或改变它的大小",
    inputs={
        "title": String(required=True, label="窗口标题"),
        "window_class": String(default="", label="窗口类名"),
        "x": Integer(default=0, label="左坐标 X"),
        "y": Integer(default=0, label="上坐标 Y"),
        "width": Integer(default=0, label="宽度", help="0 表示保持原样"),
        "height": Integer(default=0, label="高度", help="0 表示保持原样"),
        "timeout": Number(default=5.0, label="查找窗口的等待（秒）"),
    },
    outputs={
        "moved": Bool(label="是否成功"),
        "rect": List_("integer", label="移动后的矩形 [左, 上, 右, 下]"),
    },
)
def move(
    ctx: Context,
    title: str,
    window_class: str = "",
    x: int = 0,
    y: int = 0,
    width: int = 0,
    height: int = 0,
    timeout: float = 5.0,
    **_: Any,
) -> dict[str, Any]:
    window = _find(title, window_class, False, timeout)
    if window is None:
        raise RuntimeError(f"没找到窗口：{title!r}")

    left, top, right, bottom = window["rect"]
    new_width = width if width > 0 else right - left
    new_height = height if height > 0 else bottom - top

    win32gui.SetWindowPos(
        window["handle"],
        0,
        int(x),
        int(y),
        int(new_width),
        int(new_height),
        win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
    )
    rect = list(win32gui.GetWindowRect(window["handle"]))
    ctx.info(f"窗口已移动到 {rect}")
    return {"moved": True, "rect": [int(v) for v in rect]}


@action(
    id="info",
    name="窗口信息",
    category="界面",
    icon="info",
    description="读取某个窗口（默认当前前台窗口）的位置与标题",
    inputs={
        "title": String(default="", label="窗口标题", help="留空则取当前前台窗口"),
        "window_class": String(default="", label="窗口类名"),
    },
    outputs={
        "handle": Integer(label="窗口句柄"),
        "title": String(label="标题"),
        "class_name": String(label="类名"),
        "rect": List_("integer", label="矩形 [左, 上, 右, 下]"),
        "center": List_("integer", label="中心点 [x, y]"),
        "foreground": Bool(label="是否就是当前前台窗口"),
    },
)
def info(
    ctx: Context, title: str = "", window_class: str = "", **_: Any
) -> dict[str, Any]:
    if title.strip() or window_class.strip():
        window = _find(title, window_class, False, 0.0)
        if window is None:
            raise RuntimeError(f"没找到窗口：{title!r} {window_class!r}")
    else:
        handle = win32gui.GetForegroundWindow()
        if not handle:
            raise RuntimeError("当前没有前台窗口")
        window = {
            "handle": int(handle),
            "title": win32gui.GetWindowText(handle),
            "class_name": win32gui.GetClassName(handle),
            "rect": [int(v) for v in win32gui.GetWindowRect(handle)],
        }

    left, top, right, bottom = window["rect"]
    foreground = win32gui.GetForegroundWindow() == window["handle"]
    ctx.info(f"{_describe(window)}{'（前台）' if foreground else ''}")
    return {
        "handle": window["handle"],
        "title": window["title"],
        "class_name": window["class_name"],
        "rect": [int(v) for v in window["rect"]],
        "center": [(left + right) // 2, (top + bottom) // 2],
        "foreground": foreground,
    }
