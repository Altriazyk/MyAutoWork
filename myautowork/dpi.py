"""进程 DPI 感知。

**为什么必须在进程一启动就开，而且不能省。** 不开的话 Windows 会把我们的坐标系
"虚拟化"：在 125% 缩放的屏幕上，``GetSystemMetrics`` 报告 2048×1152，而屏幕截图是
2560×1440 —— 两个坐标系差 1.25 倍。后果是 UIA 给出的元素矩形和截图裁出来的区域
根本不是同一块地方，图像匹配会自信地找到一个完全错误的位置。

这个坑很隐蔽：**单看每一个 API 都"正常"**，只有把 UIA 坐标和屏幕截图放在一起才会暴露。
而且它是环境相关的 —— 在 100% 缩放的机器上永远不会犯，一换到缩放屏就全错。所以宁可
在启动时无条件开启，也不要等出了玄学问题再回来找。

设置必须在创建任何窗口之前完成；之后系统就不再允许改了（再调用会返回"拒绝访问"）。
"""

from __future__ import annotations

import ctypes
import sys

__all__ = ["enable", "current", "is_enabled", "screen_size"]

#: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_PER_MONITOR_AWARE_V2 = -4

_applied: str | None = None


def current() -> str:
    """当前进程的 DPI 感知状态。"""
    if sys.platform != "win32":
        return "not-windows"
    try:
        value = ctypes.windll.shcore.GetProcessDpiAwareness(None)
    except Exception:
        # Win7 没有 shcore，退回老接口的布尔语义
        try:
            return "system" if ctypes.windll.user32.IsProcessDPIAware() else "unaware"
        except Exception:
            return "unknown"
    return {0: "unaware", 1: "system", 2: "per-monitor"}.get(value, "unknown")


def _try_context_v2() -> bool:
    try:
        return bool(
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(_PER_MONITOR_AWARE_V2))
        )
    except Exception:
        return False


def _try_shcore() -> bool:
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        return ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0
    except Exception:
        return False


def _try_legacy() -> bool:
    try:
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except Exception:
        return False


def enable() -> str:
    """开启 DPI 感知，返回最终生效的方式。重复调用只会做一次。"""
    global _applied
    if _applied is not None:
        return _applied
    if sys.platform != "win32":
        _applied = "not-windows"
        return _applied

    for name, apply in (
        ("per-monitor-v2", _try_context_v2),
        ("per-monitor", _try_shcore),
        ("system", _try_legacy),
    ):
        if apply():
            _applied = name
            return _applied

    # 设置失败不等于没开 —— 可能已经被程序清单或别的库开好了，别把它误报成"没开"。
    state = current()
    _applied = state if state != "unaware" else "unavailable"
    return _applied


def is_enabled() -> bool:
    return enable() in ("per-monitor-v2", "per-monitor", "system")


def screen_size() -> tuple[int, int]:
    """当前进程坐标系下的屏幕尺寸。

    注意：这是**本进程看到的**尺寸。DPI 不感知时它和截图尺寸不一样，``imaging`` 会拿
    这两个数算缩放比。
    """
    if sys.platform != "win32":
        return (0, 0)
    try:
        user32 = ctypes.windll.user32
        return (int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))
    except Exception:
        return (0, 0)
