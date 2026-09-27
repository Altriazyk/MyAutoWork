"""自定义标题栏。

去掉系统标题栏，把菜单直接放到最顶上那一行 —— 那一行本来只写着一个软件名，不如让给
真正有用的东西。软件名暂时不显示。

代价是**窗口拖动和缩放要自己解决**，做法分两半：

- **拖动**：按下时调用 ``QWindow.startSystemMove()``。它走的是 Windows 原生的移动循环，
  所以贴边（Aero Snap）、拖到屏幕顶端最大化、拖回还原这些行为全都白拿 —— 自己算鼠标
  偏移是做不出这些的。
- **缩放**：在 ``MainWindow.nativeEvent`` 里接管 ``WM_NCHITTEST``，把边缘 6 像素映射成
  原生的缩放热区。同样是为了拿到原生行为（生效区域、光标形状、最大化时自动失效）。

标题栏本身只负责摆放：左边菜单，右边三个窗口按钮，中间留空。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QMenuBar, QToolButton, QWidget

__all__ = ["TitleBar"]

#: 标题栏高度。窗口按钮按这个高度撑满。
TITLE_BAR_HEIGHT = 38


class TitleBar(QWidget):
    """菜单 + 窗口按钮的顶栏。可以拖动、双击最大化。"""

    minimizeRequested = Signal()
    maximizeRequested = Signal()
    closeRequested = Signal()
    sidebarToggleRequested = Signal()

    def __init__(self, menu_bar: QMenuBar, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setFixedHeight(TITLE_BAR_HEIGHT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(0)

        # 收/放左侧栏的按钮放在菜单**前面** —— 它管的是窗口左边那一整条，放在最左
        # 才对得上位置。菜单管的是全局操作，是另一件事。
        self.sidebar_button = self._window_button("☰", "收起 / 展开左侧栏", "sidebarToggle")
        self.sidebar_button.setFixedSize(38, TITLE_BAR_HEIGHT)
        self.sidebar_button.clicked.connect(self.sidebarToggleRequested)
        layout.addWidget(self.sidebar_button)

        self.menu_bar = menu_bar
        self.menu_bar.setParent(self)
        self.menu_bar.setFixedHeight(TITLE_BAR_HEIGHT)
        layout.addWidget(self.menu_bar)
        layout.addStretch(1)

        self.min_button = self._window_button("\u2500", "最小化", "windowButton")
        self.min_button.clicked.connect(self.minimizeRequested)
        layout.addWidget(self.min_button)

        self.max_button = self._window_button("\u25a1", "最大化", "windowButton")
        self.max_button.clicked.connect(self.maximizeRequested)
        layout.addWidget(self.max_button)

        self.close_button = self._window_button("\u2715", "关闭", "closeButton")
        self.close_button.clicked.connect(self.closeRequested)
        layout.addWidget(self.close_button)

    def _window_button(self, text: str, tooltip: str, object_name: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(object_name)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setFixedSize(46, TITLE_BAR_HEIGHT)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    # -- 状态 ----------------------------------------------------------------

    def set_maximized(self, maximized: bool) -> None:
        """最大化时把图标换成"还原"，否则用户找不到怎么变回去。"""
        self.max_button.setText("\u2750" if maximized else "\u25a1")
        self.max_button.setToolTip("向下还原" if maximized else "最大化")

    # -- 交互 ----------------------------------------------------------------

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        # 只处理落在标题栏空白处的左键；点在菜单上是菜单自己的事（它是子控件，
        # 会先拿到事件）。用原生移动循环，贴边和双击最大化都是白拿的。
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximizeRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
