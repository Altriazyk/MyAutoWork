"""元素拾取器。

界面自动化能不能用，全看这一步：用户不可能手写 AutomationId（Qt 程序的 AutomationId
是「应用名.窗口名.控件名」拼出来的，实测如此），所以定位器**必须**由拾取器产出。

**交互为什么是"移动 + 点采集"，而不是"直接点目标"。** 后者更直觉，但要吞掉那次点击 ——
否则你在拾取的同时也把目标按钮按了。吞点击需要装全局鼠标钩子，那是一条又长又脆的路
（钩子回调依赖消息循环，Qt 的事件循环不一定按期待的方式派发）。现在的做法是：

1. 鼠标自由移动，一个**只画边框的透明置顶窗口**跟着光标框住它下面的元素；
2. 抬眼看 HUD 确认框对了；
3. 点 HUD 上的「采集」—— 鼠标离开目标不影响结果，因为元素是在采集那一刻、按最后一次
   悬停位置取的。

代价是多一次点击，换来的是**永远不会误触发目标界面**。对一个会真的去点鼠标键盘的工具，
这个取舍是值得的。

**边框窗口为什么是"只画边框"而不是整块半透明遮罩**：遮罩会盖住目标本身，用户看不清
自己在选什么；而且我们靠光标位置找控件，窗口绝不能挡住光标那一点。边框 + 点击穿透
（``WindowTransparentForInput``）两头都占。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from myautowork import Locator
from myautowork import dpi as _dpi
from myautowork import imaging
from myautowork import uia as bridge

from . import theme

__all__ = ["ElementPicker", "PICK_HOTKEY"]

#: 全局热键。注册失败不影响 🎯 按钮那条路。
PICK_HOTKEY = "Ctrl+Alt+P"

#: 光标轮询间隔。50ms 大约是"跟得上眼睛、又不会把 CPU 烧掉"的折中。
_POLL_MS = 50

#: 采集时把元素截图存到哪。默认放在工作目录下的 screens/，不混进 docs/。
_TEMPLATE_DIR = "screens"


class _Highlight(QWidget):
    """只画一个边框的透明置顶窗口。"""

    PAD = 3
    THICK = 3

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet("background: transparent;")

    def follow(self, rect: tuple[int, int, int, int], dpr: float) -> None:
        """rect 是物理像素（UIA 给的）；Qt 的窗口几何是逻辑像素，所以要除 DPR。"""
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            self.hide()
            return
        self.setGeometry(
            round(left / dpr) - self.PAD,
            round(top / dpr) - self.PAD,
            max(1, round((right - left) / dpr) + self.PAD * 2),
            max(1, round((bottom - top) / dpr) + self.PAD * 2),
        )
        self.show()
        self.raise_()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.NODE_BORDER_SELECTED))
        width, height, thick = self.width(), self.height(), self.THICK
        painter.drawRect(0, 0, width, thick)
        painter.drawRect(0, height - thick, width, thick)
        painter.drawRect(0, thick, thick, max(0, height - thick * 2))
        painter.drawRect(width - thick, thick, thick, max(0, height - thick * 2))


class _Hud(QWidget):
    """拾取时显示"现在框住的是什么"的小面板。故意放在屏幕角落，不挡目标。"""

    picked = Signal()
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setObjectName("pickHud")
        self.setFixedWidth(360)
        self.setStyleSheet(
            f"QWidget#pickHud {{ background: {theme.PANEL_BG};"
            f" border: 1px solid {theme.NODE_BORDER_SELECTED}; border-radius: 8px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self.heading = QLabel("正在拾取元素")
        self.heading.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(self.heading)

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        layout.addWidget(self.info)

        self.path = QLabel()
        self.path.setWordWrap(True)
        self.path.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 10px;")
        layout.addWidget(self.path)

        self.hint = QLabel(f"把鼠标移到目标上，然后点「采集」（热键 {PICK_HOTKEY}）")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("font-size: 10px;")
        layout.addWidget(self.hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.pick_button = QPushButton("采集")
        self.pick_button.setStyleSheet(
            f"QPushButton {{ background: {theme.STATE_SUCCESS}; color: #12281a;"
            " font-weight: bold; border: none; border-radius: 6px; padding: 6px 16px; }"
        )
        self.pick_button.clicked.connect(self.picked)
        buttons.addWidget(self.pick_button)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.cancelled)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    def show_info(self, lines: list[str]) -> None:
        self.info.setText("\n".join(lines) if lines else "（光标下没有可识别的元素）")

    def show_screen_corner(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.show()
            return
        area = screen.availableGeometry()
        self.adjustSize()
        self.move(area.right() - self.width() - 24, area.top() + 24)
        self.show()
        self.raise_()


class ElementPicker(QObject):
    """一次拾取会话。"""

    picked = Signal(str, object)  # (字段 key, Locator)
    cancelled = Signal()

    def __init__(self, parent: QObject | None = None, template_dir: str | Path = _TEMPLATE_DIR) -> None:
        super().__init__(parent)
        self._field_key = ""
        self._highlight = _Highlight()
        self._hud = _Hud()
        self._hud.picked.connect(self._capture)
        self._hud.cancelled.connect(self.cancel)
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._tick)
        self._last_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._template_dir = Path(template_dir)
        self._hotkey_ok = False

    # -- 生命周期 -------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    def start(self, field_key: str) -> bool:
        """开始拾取。返回是否成功启动。"""
        try:
            from myautowork import uia  # noqa: F401, PLC0415 - 提前探一下依赖在不在
        except bridge.UiaUnavailable as exc:
            self._hud.show_info([f"拾取不可用：{exc}"])
            self._hud.show_screen_corner()
            return False

        self._field_key = field_key
        self._last_rect = (0, 0, 0, 0)
        self._hud.heading.setText("正在拾取元素")
        self._hud.path.setText("")
        self._hud.hint.setText(f"把鼠标移到目标上，然后点「采集」（热键 {PICK_HOTKEY}）")
        self._hud.pick_button.setEnabled(True)
        self._hud.show_info(["把鼠标移到目标上…"])
        self._hud.show_screen_corner()
        self._timer.start()
        self._register_hotkey()
        return True

    def cancel(self) -> None:
        self._stop()
        self.cancelled.emit()

    def _stop(self) -> None:
        self._timer.stop()
        self._highlight.hide()
        self._hud.hide()
        self._unregister_hotkey()

    # -- 采样 -----------------------------------------------------------------

    def _cursor_physical(self) -> tuple[int, int]:
        """用 Win32 拿光标位置，直接就是物理像素 —— 省掉一次 DPR 换算。"""
        if sys.platform == "win32":
            try:
                point = wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
                return (int(point.x), int(point.y))
            except Exception:
                pass
        from PySide6.QtGui import QCursor  # noqa: PLC0415

        position = QCursor.pos()
        return (position.x(), position.y())

    def _tick(self) -> None:
        x, y = self._cursor_physical()
        control = bridge.control_at(x, y)
        if control is None:
            self._highlight.hide()
            self._hud.show_info(["（光标下没有可识别的元素）"])
            return

        info = bridge.control_info(control)
        self._last_rect = info["rect"]

        screen = QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen is not None else 1.0
        self._highlight.follow(info["rect"], dpr)

        # 实时预览不算路径：算路径要往上走一趟并枚举每一层的兄弟，是这里唯一有点开销的
        # 部分。采集那一刻才算，那里慢几十毫秒无所谓。
        lines = [
            f"{info['control_type'].replace('Control', '')}  {info['name'] or '(无名称)'}",
            f"AutomationId: {info['automation_id'] or '(无)'}",
            f"类名: {info['class_name'] or '(无)'}",
        ]
        self._hud.show_info(lines)
        self._hud.path.setText(
            f"位置：({info['rect'][0]},{info['rect'][1]}) "
            f"大小 {info['rect'][2] - info['rect'][0]}×{info['rect'][3] - info['rect'][1]}"
        )

    # -- 采集 -----------------------------------------------------------------

    def _capture(self) -> None:
        x, y = self._cursor_physical()
        control = bridge.control_at(x, y)
        if control is None:
            self._hud.show_info(["光标下没有可识别的元素，换个位置再试"])
            return

        screen = QGuiApplication.primaryScreen()
        screen_size = _dpi.screen_size()

        locator = bridge.locator_from_control(control, with_path=True, screen_size=screen_size)

        # 顺手把这块截图存下来 —— 它是降级链的第四级。前三级都失效时，这张图是最后的
        # 救命稻草。存失败不影响拾取本身。
        try:
            self._template_dir.mkdir(parents=True, exist_ok=True)
            stamp = f"element_{abs(hash((locator.rect, locator.name))) % 10**8:08d}.png"
            path = imaging.capture(locator.rect, self._template_dir / stamp)
            locator.image = str(path.relative_to(Path.cwd())) if path.is_relative_to(Path.cwd()) else str(path)
        except Exception:
            pass

        self._stop()
        self.picked.emit(self._field_key, locator)

    # -- 全局热键（尽力而为） --------------------------------------------------

    def _hotkey_id(self) -> int:
        return 0xA17

    def _register_hotkey(self) -> None:
        """注册全局热键。失败就算了 —— 🎯 按钮那条路不受影响。"""
        if sys.platform != "win32" or self._hotkey_ok:
            return
        try:
            modifiers = 0x0002 | 0x0001  # MOD_CONTROL | MOD_ALT
            self._hotkey_ok = bool(
                ctypes.windll.user32.RegisterHotKey(None, self._hotkey_id(), modifiers, ord("P"))
            )
        except Exception:
            self._hotkey_ok = False

    def _unregister_hotkey(self) -> None:
        if sys.platform != "win32" or not self._hotkey_ok:
            return
        try:
            ctypes.windll.user32.UnregisterHotKey(None, self._hotkey_id())
        except Exception:
            pass
        self._hotkey_ok = False

    def trigger_capture(self) -> None:
        """给外部（全局热键的 nativeEventFilter）调用。"""
        if self.active:
            self._capture()
