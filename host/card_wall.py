"""卡片墙：同一个列表，两种排法。

- **网格**：固定尺寸方砖，从左到右摆，摆不下换行（``FlowLayout``）。
- **列表**：一行一条，占满宽度。

三个页面（自动化流程 / 插件 / 我的模块）共用它，切换按钮放在**各自页面的右上角**。
"这一页怎么排"本来就是页面自己的事 —— 放到全局菜单里意味着切个页还要记得当前是哪种
排法，而且三个页面共用一个开关也不合理（插件页想用网格、流程页想用列表是完全正常的）。

切换时**重建**而不是让控件在两种排法之间变形：两者的尺寸约束根本不同（固定尺寸 vs 撑满
宽度），让一个控件同时满足两边比各排一遍还麻烦，而且状态容易在两种模式之间漏掉。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .constants import VIEW_CARD, VIEW_LIST
from .flow_layout import FlowLayout

__all__ = ["CardWall", "ViewModeToggle", "TILE_WIDTH", "TILE_HEIGHT"]

#: 网格方砖的统一尺寸。三个页面共用一套，卡片墙看起来才像同一个软件里的东西。
TILE_WIDTH = 302
TILE_HEIGHT = 184

_TOGGLE_STYLE = f"""
QPushButton {{
    background: transparent;
    color: {theme.NODE_SUBTITLE};
    border: 1px solid {theme.NODE_BORDER};
    border-radius: 6px;
    padding: 4px 12px;
    font-size: 11px;
}}
QPushButton:hover {{ color: {theme.NODE_TEXT}; border-color: {theme.NODE_BORDER_SELECTED}; }}
QPushButton:checked {{
    background: {theme.NODE_BODY_SELECTED};
    color: {theme.NODE_TEXT};
    border-color: {theme.NODE_BORDER_SELECTED};
    font-weight: bold;
}}
"""


class ViewModeToggle(QWidget):
    """页面右上角那对小按钮：网格 / 列表。"""

    changed = Signal(str)

    def __init__(self, mode: str = VIEW_CARD, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self._mode = mode

        self.grid_button = self._make_button("网格")
        self.list_button = self._make_button("列表")
        layout.addWidget(self.grid_button)
        layout.addWidget(self.list_button)

        self.grid_button.clicked.connect(lambda: self.changed.emit(VIEW_CARD))
        self.list_button.clicked.connect(lambda: self.changed.emit(VIEW_LIST))
        self.set_mode(mode)

    def _make_button(self, text: str) -> QPushButton:
        button = QPushButton(text)
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setStyleSheet(_TOGGLE_STYLE)
        button.setToolTip(f"按{'方砖网格' if text == '网格' else '一行一条'}排")
        self.group.addButton(button)
        return button

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        (self.grid_button if mode == VIEW_CARD else self.list_button).setChecked(True)

    @property
    def mode(self) -> str:
        return self._mode


class CardWall(QWidget):
    """两种排法的容器。工厂函数负责把一条数据变成方砖和行。"""

    def __init__(
        self,
        tile_factory: Callable[[Any], QWidget],
        row_factory: Callable[[Any], QWidget],
        mode: str = VIEW_CARD,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tile_factory = tile_factory
        self._row_factory = row_factory
        self._items: list[Any] = []
        self._mode = mode
        #: 列表为空时提示什么，由调用方设置。**返回字符串** —— 怎么排是容器的事，
        #: 页面不该为了"让这句话居中"去研究 FlowLayout 的脾气。
        self.hint_text: Callable[[], str] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 空态提示放在栈**外面**，而不是塞进每种排法的布局里。
        #
        # 塞进 FlowLayout 是原来那版的错：FlowLayout 按每个控件的 sizeHint 从左往右摆，
        # 提示只会贴在容器左边。给它设 AlignCenter 也没用 —— 那只是让文字在**它自己那一小块**
        # 里居中，而那一小块贴在整行的左端。
        #
        # 提示要的是"占满整行、文字居中"，那就得由容器给它整行。放栈外面天然就是整行，
        # 而且两种排法**共用同一个**，不用每次造两个。
        self.hint_label = QLabel()
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; padding: 60px 0;")
        self.hint_label.hide()
        layout.addWidget(self.hint_label)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_grid())
        self.stack.addWidget(self._build_list())
        layout.addWidget(self.stack, 1)
        self.set_view_mode(mode)

    # -- 构建 ----------------------------------------------------------------

    @staticmethod
    def _scroll() -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setStyleSheet("QScrollArea { background: transparent; }")
        return area

    def _build_grid(self) -> QWidget:
        self.grid_scroll = self._scroll()
        self.grid_host = QWidget()
        self.grid_layout = FlowLayout(self.grid_host, margin=0, h_spacing=14, v_spacing=14)
        # 让滚动区按宽度反推高度，否则卡片墙会挤成一条。
        policy = self.grid_host.sizePolicy()
        policy.setHeightForWidth(True)
        self.grid_host.setSizePolicy(policy)
        self.grid_scroll.setWidget(self.grid_host)
        return self.grid_scroll

    def _build_list(self) -> QWidget:
        self.list_scroll = self._scroll()
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 8, 0)
        self.list_layout.setSpacing(10)
        self.list_layout.addStretch(1)
        self.list_scroll.setWidget(self.list_host)
        return self.list_scroll

    # -- 数据 ----------------------------------------------------------------

    def set_items(self, items: Iterable[Any]) -> None:
        self._items = list(items)
        self.rebuild()

    def rebuild(self) -> None:
        """两页都重建。工厂函数是调用方给的，它顺手把自己的控件字典也填上。"""
        self._clear(self.grid_layout)
        self._clear(self.list_layout)

        # 空态：显示那一条整行提示，把栈整个藏起来。栈留着只会占走高度、把提示挤到一边。
        empty = not self._items and self.hint_text is not None
        if empty and self.hint_text is not None:
            self.hint_label.setText(self.hint_text())
        self.hint_label.setVisible(empty)
        self.stack.setVisible(not empty)

        for item in self._items:
            self.grid_layout.addWidget(self._tile_factory(item))
            self.list_layout.addWidget(self._row_factory(item))

        self.list_layout.addStretch(1)

    @staticmethod
    def _clear(layout: Any) -> None:
        while layout.count():
            entry = layout.takeAt(0)
            widget = entry.widget() if entry is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    # -- 排法 ----------------------------------------------------------------

    def set_view_mode(self, mode: str) -> None:
        self._mode = mode
        self.stack.setCurrentIndex(1 if mode == VIEW_LIST else 0)

    @property
    def view_mode(self) -> str:
        return self._mode

    @property
    def items(self) -> list[Any]:
        return list(self._items)
