"""首页：自动化流程列表。

这是用户打开软件看到的第一屏，也是一天里用得最多的一屏。它应该像**发射台**，
不像编辑器：一眼看到有哪些自动化，点一下就执行。

两种展示方式（切换在顶部「视图」菜单里）：

- **卡片**：网格排列。卡片固定尺寸，从左到右摆、摆不下换行 —— 窗口拉宽就多放一张。
  适合流程不多、想看清楚每条在干什么的时候。
- **列表**：一行一条，占满宽度。信息还是那些，但一屏能扫十几条，适合对着"上次运行结果"
  过一遍。

两者是同一份数据的两种排法，共用基类里的状态徽标、运行态、双击执行逻辑，所以行为完全
一致 —— 不是两套实现。

一个刻意的取舍：**单击只是选中，运行要按「运行」或双击。** 自动化一旦跑起来会真的去动
鼠标键盘、真的写文件，让它被一次误点触发是不可接受的。"一键执行"省掉的是"打开画布再按
F5"，不是省掉所有确认。

还有一个必须守住的底线：**任何一条流程解析失败，都不能让整页打不开。** 那多半是别人手改
坏了 JSON，或者是引用了没装的插件 —— 这时候用户最需要的是看到"第三条坏了、原因是这个"，
而不是一个空白页面。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from kernel.library import WorkflowEntry, WorkflowLibrary

from . import theme
from .card_wall import TILE_HEIGHT, TILE_WIDTH, ViewModeToggle
from .constants import VIEW_CARD, VIEW_LIST
from .flow_layout import FlowLayout

__all__ = ["WorkflowCard", "WorkflowRow", "WorkflowListView"]

_STATUS_TEXT = {
    "success": "成功",
    "failed": "失败",
    "cancelled": "已停止",
    "running": "运行中",
}
_STATUS_COLORS = {
    "success": theme.STATE_SUCCESS,
    "failed": theme.STATE_FAILED,
    "cancelled": theme.STATE_SKIPPED,
    "running": theme.STATE_RUNNING,
}

#: 网格卡片的固定尺寸。宽度固定，描述的两行折行就能一次算好，不用在 resize 里重算。
TILE_WIDTH = 302
TILE_HEIGHT = 180

#: 网格卡片的标题行要给状态徽标留出的宽度。
_BADGE_RESERVE = 66

_CARD_STYLE = f"""
QFrame#entryFrame {{
    background: {theme.NODE_BODY};
    border: 1px solid {theme.NODE_BORDER};
    border-radius: 10px;
}}
QFrame#entryFrame:hover {{
    background: {theme.NODE_BODY_SELECTED};
    border: 1px solid {theme.NODE_BORDER_SELECTED};
}}
"""

_RUN_BUTTON_STYLE = f"""
QPushButton {{
    background: {theme.STATE_SUCCESS};
    color: #12281a;
    font-weight: bold;
    border: none;
    border-radius: 6px;
    padding: 7px 16px;
}}
QPushButton:hover {{ background: #5fc074; }}
QPushButton:disabled {{ background: #3a4149; color: #6b7684; }}
"""

#: 「停止」：实心橙。它跟「运行」占同一个位置，所以形状要一致；颜色必须**明显不同** ——
#: 用户是在"再点一次刚才那个按钮"，得让他一眼看出这一下点下去不是再跑一遍。
_STOP_BUTTON_STYLE = f"""
QPushButton {{
    background: {theme.STATE_FAILED};
    color: #ffffff;
    font-weight: bold;
    border: none;
    border-radius: 6px;
    padding: 7px 16px;
}}
QPushButton:hover {{ background: #e2685f; }}
QPushButton:disabled {{ background: #3a4149; color: #6b7684; }}
"""

#: 卡片上那一行按钮的统一高度。度量在 theme 里，两个页面共用 —— 见那里的说明。
_CARD_BUTTON_HEIGHT = theme.ROW_BUTTON_HEIGHT

#: 删除按钮。**描边而不是实心** —— 这是破坏性操作，不该长得跟「运行」一样显眼。
#: 它在按钮行里也单独隔开一段距离，手滑的概率能小一点。
_DELETE_BUTTON_STYLE = f"""
QPushButton {{
    background: transparent;
    color: {theme.STATE_FAILED};
    border: 1px solid {theme.STATE_FAILED};
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ background: {theme.STATE_FAILED}; color: #1b1f25; }}
QPushButton:disabled {{ border-color: #3a4149; color: #6b7684; background: transparent; }}
"""


def _status_badge(text: str) -> str:
    return _STATUS_TEXT.get(text, text) if text else ""


def _elide_lines(text: str, metrics: QFontMetrics, width: int, lines: int) -> str:
    """把文本折成最多 ``lines`` 行，放不下就用省略号收尾。

    中文没有空格，按字符宽度二分找折点；英文会断在单词中间，但在卡片这种小面积里
    影响很小，换来的是实现简单、且不会出现半截行被高度裁掉。
    """
    text = " ".join(text.split())
    if not text:
        return ""
    out: list[str] = []
    rest = text
    for index in range(lines):
        if not rest:
            break
        low, high = 0, len(rest)
        while low < high:
            mid = (low + high + 1) // 2
            if metrics.horizontalAdvance(rest[:mid]) <= width:
                low = mid
            else:
                high = mid - 1
        take = low
        if take >= len(rest):
            out.append(rest)
            break
        if index == lines - 1:
            while take > 0 and metrics.horizontalAdvance(rest[:take] + "…") > width:
                take -= 1
            out.append(rest[:take] + "…")
            break
        out.append(rest[:take])
        rest = rest[take:]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 两种排法共用的部分
# --------------------------------------------------------------------------- #


class EntryFrame(QFrame):
    """一条流程的外框。状态徽标、运行态、双击执行都在这里，两种排法共用。"""

    runRequested = Signal(object)
    editRequested = Signal(object)
    revealRequested = Signal(object)
    deleteRequested = Signal(object)
    stopRequested = Signal(object)

    def __init__(self, entry: WorkflowEntry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self._running = False

        self.setObjectName("entryFrame")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setStyleSheet(_CARD_STYLE)
        self.setToolTip(str(entry.path))

        self.title = QLabel()
        self.badge = QLabel()
        self.badge.setVisible(False)
        self.description = QLabel()
        self.meta = QLabel()

        self.run_button = QPushButton("运行")
        self.run_button.setStyleSheet(_RUN_BUTTON_STYLE)
        self.run_button.setToolTip("立即执行这条自动化流程")
        self.run_button.clicked.connect(self._on_run_clicked)

        self.edit_button = QPushButton("编辑")
        self.edit_button.setToolTip("在画布上打开")
        self.edit_button.clicked.connect(lambda: self.editRequested.emit(self.entry))

        self.reveal_button = QPushButton("位置")
        self.reveal_button.setToolTip("在资源管理器里打开所在目录")
        self.reveal_button.clicked.connect(lambda: self.revealRequested.emit(self.entry))

        self.delete_button = QPushButton("删除")
        self.delete_button.setStyleSheet(_DELETE_BUTTON_STYLE)
        self.delete_button.setToolTip("删到回收站，误删还能捞回来")
        self.delete_button.clicked.connect(lambda: self.deleteRequested.emit(self.entry))

    # -- 内容 ----------------------------------------------------------------

    def _load_entry_text(self, title_width: int, description_width: int, description_lines: int) -> None:
        """把条目内容填进控件。两种排法只有宽度和行数不同。"""
        self.title.setText(
            _elide_lines(
                self.entry.display_name,
                QFontMetrics(self.title.font()),
                title_width,
                1,
            )
        )

        has_description = bool(self.entry.description)
        source = self.entry.description if has_description else "（没有填写描述）"
        if not self.entry.ok:
            source = f"无法解析：{self.entry.error}"
        self.description.setText(
            _elide_lines(source, QFontMetrics(self.description.font()), description_width, description_lines)
        )
        self.description.setStyleSheet(
            f"color: {theme.STATE_FAILED};"
            if not self.entry.ok
            else f"color: {theme.NODE_SUBTITLE if has_description else '#5c6674'};"
        )
        self.description.setToolTip(source)

        if not self.entry.ok:
            self.meta.setText("")
            self.badge.setVisible(False)
            self.run_button.setEnabled(False)
            self.run_button.setToolTip("这条流程读不出来，修好之后才能运行")
            return

        self.run_button.setToolTip("立即执行这条自动化流程")
        status = self.entry.last_status
        base = f"{self.entry.node_count} 个节点 · {self.entry.edge_count} 条连线"
        if status:
            tail = f"上次运行：{_status_badge(status)}"
            if self.entry.last_duration_ms:
                tail += f" · {self.entry.last_duration_ms / 1000:.2f} 秒"
        else:
            tail = "尚未运行过"
        self.meta.setText(f"{base}\n{tail}")
        self._update_badge(status)

    def refresh(self, entry: WorkflowEntry | None = None) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    # -- 运行状态 -------------------------------------------------------------

    def set_running(self, running: bool) -> None:
        """跑起来之后，「运行」就地变成「停止」。

        **不另加一个按钮。** 方砖只有 302 宽，四个按钮已经排满；而且「运行」和「停止」是
        同一个位置上的两个状态，本来就该是同一个按钮 —— 分成两个反而要用户去找哪个能点。
        """
        self._running = running
        if running:
            self._paint_badge("运行中", theme.STATE_RUNNING)
            self.run_button.setText("停止")
            self.run_button.setStyleSheet(_STOP_BUTTON_STYLE)
            self.run_button.setToolTip("让当前节点跑完就停下 —— 不是立刻掐断")
            self.run_button.setEnabled(True)
        else:
            self._update_badge(self.entry.last_status)
            self.run_button.setText("运行")
            self.run_button.setStyleSheet(_RUN_BUTTON_STYLE)
            self.run_button.setToolTip("立即执行这条自动化流程")
            self.run_button.setEnabled(self.entry.ok)

    def set_actions_enabled(self, enabled: bool) -> None:
        """有任务在跑时，把**别的**条目的按钮禁掉 —— 同时只允许跑一条。

        「运行」按钮不在这里管：跑着的那条要变成「停止」并且**必须保持可点**，
        否则用户就没法停下来了。它由 ``set_running`` 控制。
        """
        self.edit_button.setEnabled(enabled)
        self.reveal_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)
        if not self._running:
            self.run_button.setEnabled(enabled and self.entry.ok)

    def _on_run_clicked(self) -> None:
        """一个按钮，两个身份。"""
        if self._running:
            self.stopRequested.emit(self.entry)
        else:
            self.runRequested.emit(self.entry)

    def _update_badge(self, status: str) -> None:
        if self._running:
            self._paint_badge("运行中", theme.STATE_RUNNING)
            return
        if not status:
            self.badge.setVisible(False)
            return
        self._paint_badge(_status_badge(status), _STATUS_COLORS.get(status, theme.NODE_SUBTITLE))

    def _paint_badge(self, text: str, color: str) -> None:
        self.badge.setText(f" {text} ")
        self.badge.setStyleSheet(
            f"background: {color}; color: #1b1f25; border-radius: 7px;"
            "font-size: 11px; font-weight: bold; padding: 1px 6px;"
        )
        self.badge.setVisible(True)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        if self.entry.ok and self.run_button.isEnabled():
            self.runRequested.emit(self.entry)
        super().mouseDoubleClickEvent(event)


# --------------------------------------------------------------------------- #
# 卡片：网格排列
# --------------------------------------------------------------------------- #


class WorkflowCard(EntryFrame):
    """网格视图里的一块方砖。"""

    def __init__(self, entry: WorkflowEntry, parent: QWidget | None = None) -> None:
        super().__init__(entry, parent)
        self.setFixedSize(TILE_WIDTH, TILE_HEIGHT)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(6)

        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title_font = QFont()
        title_font.setPointSizeF(11.0)
        title_font.setBold(True)
        self.title.setFont(title_font)
        title_row.addWidget(self.title, 1)
        title_row.addWidget(self.badge)
        outer.addLayout(title_row)

        self.description.setWordWrap(True)
        self.description.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.description.setFixedHeight(38)
        outer.addWidget(self.description)

        outer.addStretch(1)

        self.meta.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        outer.addWidget(self.meta)

        # 四个按钮**等宽平铺整行**。删除不再单拎出去 —— 它跟另外三个并排，红色的描边已经
        # 足够区分，再拉开一段距离反而让这一行看着断成两截。
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        for button in (
            self.run_button,
            self.edit_button,
            self.reveal_button,
            self.delete_button,
        ):
            # Expanding + 相同的 stretch，四个就自动等宽占满整行，不用去算每种的宽度。
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setFixedHeight(_CARD_BUTTON_HEIGHT)
            buttons.addWidget(button, 1)
        outer.addLayout(buttons)

        self.refresh()

    def refresh(self, entry: WorkflowEntry | None = None) -> None:
        if entry is not None:
            self.entry = entry
        self._load_entry_text(
            TILE_WIDTH - 28 - _BADGE_RESERVE,
            TILE_WIDTH - 28,
            2,
        )


# --------------------------------------------------------------------------- #
# 列表：一行一条
# --------------------------------------------------------------------------- #


class WorkflowRow(EntryFrame):
    """列表视图里的一条。占满整行，信息横向铺开。"""

    def __init__(self, entry: WorkflowEntry, parent: QWidget | None = None) -> None:
        super().__init__(entry, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 14, 12)
        outer.setSpacing(5)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_font = QFont()
        title_font.setPointSizeF(11.5)
        title_font.setBold(True)
        self.title.setFont(title_font)
        title_row.addWidget(self.title)
        title_row.addWidget(self.badge)
        title_row.addStretch(1)
        outer.addLayout(title_row)

        self.description.setWordWrap(True)
        outer.addWidget(self.description)

        self.meta.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        self.meta.setWordWrap(True)
        # meta 自己占一行，别和按钮抢宽度 —— 它长短不一，挤在同一行会把按钮推得忽宽忽窄。
        outer.addWidget(self.meta)

        # 四个按钮等宽平铺整行，和卡片视图一致。「删除」也不再单独隔开，靠红色描边区分。
        # 注意 self.delete_button 一直存在（定义在共享的 EntryFrame 里），只是**从来没被
        # 摆进这一行的布局** —— 所以列表视图一直没有删除按钮。
        buttons = QHBoxLayout()
        buttons.setSpacing(theme.ROW_BUTTON_SPACING)
        for button in (
            self.run_button,
            self.edit_button,
            self.reveal_button,
            self.delete_button,
        ):
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setFixedHeight(_CARD_BUTTON_HEIGHT)
            buttons.addWidget(button, 1)
        outer.addLayout(buttons)

        self.refresh()

    def refresh(self, entry: WorkflowEntry | None = None) -> None:
        if entry is not None:
            self.entry = entry
        # 通栏卡片宽度随窗口变，所以描述不折行（让它自己 wordWrap），只算标题。
        width = max(240, self.width() - 28) if self.width() > 40 else 720
        self._load_entry_text(width - _BADGE_RESERVE, width, 3)
        # 通栏的元信息排成一行更好读
        self.meta.setText(self.meta.text().replace("\n", "   ·   "))


# --------------------------------------------------------------------------- #
# 首页整体
# --------------------------------------------------------------------------- #


class WorkflowListView(QWidget):
    """首页整体：网格 / 列表两种排法。"""

    runRequested = Signal(object)
    editRequested = Signal(object)
    revealRequested = Signal(object)
    deleteRequested = Signal(object)
    stopRequested = Signal(object)

    def __init__(self, library: WorkflowLibrary, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.library = library
        self._cards: dict[Path, WorkflowCard] = {}
        self._rows: dict[Path, WorkflowRow] = {}
        self._entries: list[WorkflowEntry] = []
        self._running_path: Path | None = None
        self._mode = VIEW_CARD

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # --- 顶部：页面名 + 统计 + 搜索 ---
        # 「新建 / 打开」在左侧导航栏，「展示方式」在顶部「视图」菜单，这里只留定位用的。
        header = QHBoxLayout()
        header.setSpacing(10)

        self.heading = QLabel("我的流程")
        heading_font = QFont()
        heading_font.setPointSizeF(14.0)
        heading_font.setBold(True)
        self.heading.setFont(heading_font)
        header.addWidget(self.heading)

        self.summary = QLabel()
        self.summary.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        header.addWidget(self.summary)
        header.addStretch(1)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索流程…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(lambda _: self.apply_filter())
        header.addWidget(self.search)

        # 切换按钮在这一页的右上角 —— "这一页怎么排"是页面自己的事。
        self.toggle = ViewModeToggle()
        self.toggle.changed.connect(self.set_view_mode)
        header.addWidget(self.toggle)

        layout.addLayout(header)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_grid_page())  # 0
        self.stack.addWidget(self._build_list_page())  # 1
        layout.addWidget(self.stack, 1)

        self.set_view_mode(VIEW_CARD)

    # -- 构建 ----------------------------------------------------------------

    def _make_scroll(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        return scroll

    def _build_grid_page(self) -> QWidget:
        self.grid_scroll = self._make_scroll()
        self.grid_host = QWidget()
        # 外面套一层竖直布局，空态提示才有地方"占满整行"。
        #
        # 原来是把提示直接塞进 FlowLayout，还给了个 setFixedWidth(560) —— 那是错的。
        # FlowLayout 按每个控件的 sizeHint 从左往右摆，那 560 像素只会贴在容器左边；
        # 给它设 AlignCenter 也没用，那只让文字在**它自己那一小块**里居中。
        # 窗口一宽，看起来就是左对齐。
        outer = QVBoxLayout(self.grid_host)
        outer.setContentsMargins(0, 0, 0, 0)
        self.grid_hint = QLabel()
        self.grid_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.grid_hint.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; padding: 80px 0;")
        self.grid_hint.hide()
        outer.addWidget(self.grid_hint)

        # 流式布局：卡片从左到右摆，摆不下自动换行 —— 窗口拉宽就多放一张。
        self.grid_layout = FlowLayout(margin=0, h_spacing=14, v_spacing=14)
        outer.addLayout(self.grid_layout)
        outer.addStretch(1)

        # 让滚动区按宽度反推高度，否则卡片墙会挤成一条。
        policy = self.grid_host.sizePolicy()
        policy.setHeightForWidth(True)
        self.grid_host.setSizePolicy(policy)
        self.grid_scroll.setWidget(self.grid_host)
        return self.grid_scroll

    def _build_list_page(self) -> QWidget:
        self.list_scroll = self._make_scroll()
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 8, 0)
        self.list_layout.setSpacing(10)
        self.list_layout.addStretch(1)
        self.list_scroll.setWidget(self.list_host)
        return self.list_scroll

    # -- 数据 ----------------------------------------------------------------

    def reload(self) -> None:
        """重新扫描磁盘。"""
        self.library.discover()
        self.apply_filter()

    def apply_filter(self) -> None:
        """只按当前关键字过滤，不重新读盘。"""
        self._entries = self.library.filtered(self.search.text())
        self._rebuild_grid()
        self._rebuild_list()
        self._update_summary()

    def update_entry(self, entry: WorkflowEntry) -> None:
        """运行结束后只刷新那一条，避免整页重建、滚动位置跳掉。

        也要替换 ``self._entries`` 里的那一份 —— 它是"当前筛选结果"的快照，
        之后任何一次重建（比如运行结束时清掉"运行中"标记）都会读它。
        """
        for index, existing in enumerate(self._entries):
            if existing.path == entry.path:
                self._entries[index] = entry
                break
        card = self._cards.get(entry.path)
        if card is not None:
            card.refresh(entry)
        row = self._rows.get(entry.path)
        if row is not None:
            row.refresh(entry)

    def _clear_layout(self, layout: Any) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _empty_hint(self) -> str:
        return (
            "没有匹配的流程，换个关键字试试。"
            if self.search.text().strip()
            else "还没有任何流程。点左侧「新建流程」开始。"
        )

    def _rebuild_grid(self) -> None:
        self._clear_layout(self.grid_layout)
        self._cards.clear()

        if not self._entries:
            # 提示在流式布局外面（见 _build_grid_page），占满整行，文字自然居中。
            self.grid_hint.setText(self._empty_hint())
            self.grid_hint.show()
            return
        self.grid_hint.hide()

        for entry in self._entries:
            card = WorkflowCard(entry)
            card.runRequested.connect(self.runRequested)
            card.editRequested.connect(self.editRequested)
            card.revealRequested.connect(self.revealRequested)
            card.deleteRequested.connect(self.deleteRequested)
            card.stopRequested.connect(self.stopRequested)
            self._apply_running_state(card, entry)
            self.grid_layout.addWidget(card)
            self._cards[entry.path] = card

    def _rebuild_list(self) -> None:
        self._clear_layout(self.list_layout)
        self._rows.clear()

        if not self._entries:
            hint = QLabel(self._empty_hint())
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; padding: 80px 0;")
            self.list_layout.addWidget(hint)
        else:
            for entry in self._entries:
                row = WorkflowRow(entry)
                row.runRequested.connect(self.runRequested)
                row.editRequested.connect(self.editRequested)
                row.revealRequested.connect(self.revealRequested)
                row.deleteRequested.connect(self.deleteRequested)
                row.stopRequested.connect(self.stopRequested)
                self._apply_running_state(row, entry)
                self.list_layout.addWidget(row)
                self._rows[entry.path] = row

        self.list_layout.addStretch(1)

    def _apply_running_state(self, widget: EntryFrame, entry: WorkflowEntry) -> None:
        if self._running_path is None:
            return
        widget.set_actions_enabled(False)
        if entry.path == self._running_path:
            widget.set_running(True)

    def _update_summary(self) -> None:
        total = len(self.library.entries)
        broken = sum(1 for entry in self.library.entries if not entry.ok)
        text = f"共 {total} 条"
        if broken:
            text += f"，其中 {broken} 条无法解析"
        self.summary.setText(text)

    # -- 展示方式 -------------------------------------------------------------

    def set_view_mode(self, mode: str) -> None:
        self._mode = mode
        self.stack.setCurrentIndex(1 if mode == VIEW_LIST else 0)
        self.toggle.set_mode(mode)
        if mode == VIEW_LIST:
            # 通栏卡片的标题要在知道实际宽度之后才能折行，切过来时补算一次。
            for _entry_path, row in self._rows.items():
                row.refresh()

    @property
    def view_mode(self) -> str:
        return self._mode

    # -- 运行状态 -------------------------------------------------------------

    def set_running(self, path: str | Path | None) -> None:
        self._running_path = Path(path) if path else None
        for widget_map in (self._cards, self._rows):
            for widget_path, widget in widget_map.items():
                widget.set_running(widget_path == self._running_path)
                widget.set_actions_enabled(self._running_path is None)

    @property
    def running_path(self) -> Path | None:
        return self._running_path

    def card_for(self, path: str | Path) -> WorkflowCard | None:
        return self._cards.get(Path(path))

    def row_for(self, path: str | Path) -> WorkflowRow | None:
        return self._rows.get(Path(path))

    @property
    def entries(self) -> list[WorkflowEntry]:
        return list(self._entries)
