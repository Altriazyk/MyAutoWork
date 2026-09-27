"""清单：把一批流程编成一份"今天要做的"，按顺序跑完。

**为什么拆成两页。** 「我的清单」只管**列出已有的清单**（和「我的模块」页一个形态），
新建/编辑在单独一页做。挤在一页里的话，"新建"和"我现在在看哪条"会互相打架：列表得同时
承担选择器和编辑区两个职责，用户也分不清下面那些勾选框到底属于哪条清单。

**运行是严格串行的，不是并发。** 界面自动化会抢鼠标键盘、文件流程会抢同一个文件 —— 两条
流程同时跑，结果多半是互相踩。而且顺序本身有意义（先导出再上传），并发直接把这个语义丢
掉了。所以每一条清单都自带「运行」：点哪条跑哪条，不用先切过去。

**勾选状态存盘。** 一份"每天要做的事"如果每次重启软件都得重新勾一遍，那还不如写在便签上。
存的是流程**文件名**而不是绝对路径 —— 清单描述的是"要做什么"，不是"文件在硬盘哪个角落"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from kernel.checklist import (
    CHECKLIST_FILE,
    DEFAULT_CHECKLIST,
    ChecklistSettings,
    load_checklists,
    save_checklists,
)
from kernel.library import WorkflowEntry

from . import theme

__all__ = [
    "ChecklistView",
    "ChecklistEditorView",
    "ChecklistRow",
    "ChecklistItemRow",
    "load_checklists",
    "save_checklists",
    "DEFAULT_CHECKLIST",
]

#: 删除按钮：描边而不是实心 —— 破坏性操作不该长得跟「运行」一样显眼。
_DELETE_STYLE = f"""
QPushButton {{
    background: transparent; color: {theme.STATE_FAILED};
    border: 1px solid {theme.STATE_FAILED}; border-radius: 6px; padding: 5px 12px;
}}
QPushButton:hover {{ background: {theme.STATE_FAILED}; color: #1b1f25; }}
QPushButton:disabled {{ border-color: #3a4149; color: #6b7684; background: transparent; }}
"""

_RUN_STYLE = f"""
QPushButton {{
    background: {theme.STATE_SUCCESS}; color: #12281a;
    font-weight: bold; border: none; border-radius: 6px; padding: 5px 14px;
}}
QPushButton:hover {{ background: #5fc074; }}
QPushButton:disabled {{ background: #3a4149; color: #6b7684; }}
"""


class ChecklistSettingsDialog(QDialog):
    """一条清单自己的设置。**现在只有「开机运行」一项。**

    以后加设置项就往下加一行，不用动别的地方 —— 这页是"这条清单自己的事"，跟运行方式
    （勾了哪些流程）分开，免得一个页面同时管两件事。
    """

    def __init__(
        self,
        name: str,
        settings: ChecklistSettings,
        startup_available: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"清单设置 · {name}")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        heading = QLabel(f"「{name}」的设置")
        font = QFont()
        font.setPointSizeF(12.0)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)

        self.startup = QCheckBox("开机后自动运行这条清单")
        # 注册表里没这一项时，就算配置文件写着 true 也不勾上 —— **以注册表为准**，
        # 否则界面会显示"已开启"而实际什么都不会发生。
        self.startup.setChecked(bool(settings.startup) and startup_available)
        self.startup.setEnabled(startup_available)
        layout.addWidget(self.startup)

        note = QLabel(
            (
                "开机之后在后台按顺序跑完整条清单，**不弹窗口**。\n"
                "改的是当前用户的启动项（不需要管理员权限），随时可以在这里关掉。\n"
                "系统登录后才会触发；锁屏没登录的话不会跑。"
            )
            if startup_available
            else "开机运行目前只在 Windows 上实现。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def startup_enabled(self) -> bool:
        return self.startup.isChecked()


class ChecklistRow(QFrame):
    """编辑器里的一行：勾选框 + 流程名 + 状态。"""

    toggled = Signal(str, bool)

    def __init__(self, entry: WorkflowEntry, checked: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self.setObjectName("checkRow")
        self.setStyleSheet(
            f"QFrame#checkRow {{ background: {theme.NODE_BODY};"
            f" border: 1px solid {theme.NODE_BORDER}; border-radius: 8px; }}"
        )
        self.setToolTip(str(entry.path))

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 9, 12, 9)
        row.setSpacing(10)

        self.box = QCheckBox()
        self.box.setChecked(checked)
        # 坏掉的流程不给勾 —— 勾了也跑不起来，只会在执行时白报一次错。
        self.box.setEnabled(entry.ok)
        if not entry.ok:
            self.box.setToolTip("这条流程读不出来，修好才能加进清单")
        self.box.toggled.connect(
            lambda state: self.toggled.emit(str(self.entry.path), bool(state))
        )
        row.addWidget(self.box)

        text = QVBoxLayout()
        text.setSpacing(1)
        name = QLabel(entry.display_name)
        font = QFont()
        font.setPointSizeF(10.5)
        font.setBold(True)
        name.setFont(font)
        text.addWidget(name)

        if not entry.ok:
            detail, color = f"无法解析：{entry.error}", theme.STATE_FAILED
        else:
            detail, color = entry.description or "（没有填写描述）", theme.NODE_SUBTITLE
        subtitle = QLabel(detail)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color: {color}; font-size: 11px;")
        text.addWidget(subtitle)
        row.addLayout(text, 1)

        self.status = QLabel()
        row.addWidget(self.status)
        self.set_status("", "")

    def set_status(self, text: str, kind: str) -> None:
        colors = {
            "running": theme.STATE_RUNNING,
            "success": theme.STATE_SUCCESS,
            "failed": theme.STATE_FAILED,
        }
        self.status.setText(text)
        self.status.setStyleSheet(
            f"color: {colors.get(kind, theme.NODE_SUBTITLE)}; font-size: 11px;"
        )

    @property
    def checked(self) -> bool:
        return self.box.isChecked()

    def set_checked(self, value: bool) -> None:
        self.box.setChecked(value)


class ChecklistItemRow(QFrame):
    """「我的清单」列表里的一条：名字 + 几条流程 + 运行/编辑/重命名/删除。"""

    runRequested = Signal(str)
    editRequested = Signal(str)
    renameRequested = Signal(str)
    deleteRequested = Signal(str)
    settingsRequested = Signal(str)

    def __init__(self, name: str, count: int, current: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.setObjectName("checkItemRow")
        self.setStyleSheet(
            f"QFrame#checkItemRow {{ background: "
            f"{theme.NODE_BODY_SELECTED if current else theme.NODE_BODY};"
            f" border: 1px solid "
            f"{theme.NODE_BORDER_SELECTED if current else theme.NODE_BORDER};"
            " border-radius: 8px; }"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 9, 10, 9)
        row.setSpacing(8)

        label = QLabel(name)
        font = QFont()
        font.setPointSizeF(11.0)
        font.setBold(current)
        label.setFont(font)
        row.addWidget(label)

        self.counts = QLabel()
        self.counts.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        row.addWidget(self.counts)

        self.status = QLabel()
        row.addWidget(self.status)
        row.addStretch(1)

        self.run_button = QPushButton("运行")
        self.run_button.setStyleSheet(_RUN_STYLE)
        self.run_button.clicked.connect(lambda: self.runRequested.emit(name))
        row.addWidget(self.run_button)

        self.edit_button = QPushButton("编辑")
        self.edit_button.setToolTip("改名字，或者增减它包含的流程")
        self.edit_button.clicked.connect(lambda: self.editRequested.emit(name))
        row.addWidget(self.edit_button)

        self.rename_button = QPushButton("重命名")
        self.rename_button.clicked.connect(lambda: self.renameRequested.emit(name))
        row.addWidget(self.rename_button)

        self.settings_button = QPushButton("设置")
        self.settings_button.setToolTip("这条清单自己的设置（开机运行等）")
        self.settings_button.clicked.connect(lambda: self.settingsRequested.emit(name))
        row.addWidget(self.settings_button)

        self.delete_button = QPushButton("删除")
        self.delete_button.setStyleSheet(_DELETE_STYLE)
        self.delete_button.setToolTip("只删这份清单，流程本身不会被删")
        self.delete_button.clicked.connect(lambda: self.deleteRequested.emit(name))
        row.addWidget(self.delete_button)

        self.set_count(count)
        self.set_status("", "")

    def set_count(self, count: int) -> None:
        self.counts.setText(f"{count} 条流程")
        self.run_button.setEnabled(count > 0)
        self.run_button.setToolTip("按顺序跑完这条清单里的流程" if count else "这条清单还是空的")

    def set_status(self, text: str, kind: str) -> None:
        colors = {
            "running": theme.STATE_RUNNING,
            "success": theme.STATE_SUCCESS,
            "failed": theme.STATE_FAILED,
        }
        self.status.setText(text)
        self.status.setStyleSheet(
            f"color: {colors.get(kind, theme.NODE_SUBTITLE)}; font-size: 11px;"
        )


class ChecklistEditorView(QWidget):
    """清单编辑器：起名字 + 勾选它包含哪些流程。"""

    saved = Signal(str, str, list)  # (原名，新名，[流程文件名]；新建时原名为空)
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._editing = ""
        self._rows: dict[str, ChecklistRow] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)
        self.heading = QLabel("新建清单")
        font = QFont()
        font.setPointSizeF(14.0)
        font.setBold(True)
        self.heading.setFont(font)
        header.addWidget(self.heading)

        self.subtitle = QLabel("勾选这条清单要跑哪些流程")
        self.subtitle.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        header.addWidget(self.subtitle)
        header.addStretch(1)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.cancelled)
        header.addWidget(self.cancel_button)

        self.save_button = QPushButton("保存清单")
        self.save_button.setStyleSheet(_RUN_STYLE)
        self.save_button.clicked.connect(self.save)
        header.addWidget(self.save_button)
        layout.addLayout(header)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addWidget(QLabel("名字"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("比如：早上这一套")
        self.name_edit.textChanged.connect(lambda _: self._revalidate())
        name_row.addWidget(self.name_edit, 1)

        self.select_all = QPushButton("全选")
        self.select_all.clicked.connect(lambda: self._set_all(True))
        name_row.addWidget(self.select_all)
        self.clear_all = QPushButton("全不选")
        self.clear_all.clicked.connect(lambda: self._set_all(False))
        name_row.addWidget(self.clear_all)
        layout.addLayout(name_row)

        self.summary = QLabel()
        self.summary.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        layout.addWidget(self.summary)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.host = QWidget()
        self.rows_layout = QVBoxLayout(self.host)
        self.rows_layout.setContentsMargins(0, 0, 8, 0)
        self.rows_layout.setSpacing(8)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(self.host)
        layout.addWidget(self.scroll, 1)

    # -- 进入方式 -------------------------------------------------------------

    def start_new(self, entries: list[WorkflowEntry]) -> None:
        self._editing = ""
        self.heading.setText("新建清单")
        self.name_edit.clear()
        self._fill(entries, set())

    def load(self, name: str, entries: list[WorkflowEntry], picked: set[str]) -> None:
        self._editing = name
        self.heading.setText(f"编辑清单 · {name}")
        self.name_edit.setText(name)
        self._fill(entries, picked)

    def _fill(self, entries: list[WorkflowEntry], picked: set[str]) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._rows.clear()

        if not entries:
            empty = QLabel("还没有任何流程。先去「我的流程」页新建一条。")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; padding: 60px 0;")
            self.rows_layout.addWidget(empty)
        else:
            for entry in entries:
                row = ChecklistRow(entry, entry.path.name in picked)
                row.toggled.connect(lambda _p, _s: self._refresh_summary())
                self.rows_layout.addWidget(row)
                self._rows[entry.path.name] = row

        self.rows_layout.addStretch(1)
        self._refresh_summary()

    @property
    def editing(self) -> str:
        return self._editing

    def picked_names(self) -> list[str]:
        return [name for name, row in self._rows.items() if row.checked]

    def row_for(self, name: str) -> ChecklistRow | None:
        return self._rows.get(name)

    def focus_name(self) -> None:
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def show_name_error(self, message: str) -> None:
        self.name_edit.setPlaceholderText(message)
        self.name_edit.setFocus()

    # -- 内部 -----------------------------------------------------------------

    def _set_all(self, value: bool) -> None:
        for row in self._rows.values():
            if value and not row.entry.ok:
                continue
            row.set_checked(value)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        self.summary.setText(
            f"共 {len(self._rows)} 条可选，已勾选 {len(self.picked_names())} 条"
        )
        self._revalidate()

    def _revalidate(self) -> None:
        self.save_button.setEnabled(bool(self.name_edit.text().strip()))

    def save(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self._revalidate()
            return
        self.saved.emit(self._editing, name, self.picked_names())


class ChecklistView(QWidget):
    """「我的清单」：**只列出已有的清单**，新建和编辑都在别的页面。"""

    newRequested = Signal()
    runRequested = Signal(str)
    editRequested = Signal(str)
    renameRequested = Signal(str)
    deleteRequested = Signal(str)
    settingsRequested = Signal(str)
    stopRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: dict[str, ChecklistItemRow] = {}
        self._current = ""
        self._running = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)
        heading = QLabel("我的清单")
        font = QFont()
        font.setPointSizeF(14.0)
        font.setBold(True)
        heading.setFont(font)
        header.addWidget(heading)

        self.summary = QLabel()
        self.summary.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        header.addWidget(self.summary)
        header.addStretch(1)

        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stopRequested)
        header.addWidget(self.stop_button)

        self.new_button = QPushButton("新建清单")
        self.new_button.setToolTip("打开清单编辑器")
        self.new_button.clicked.connect(self.newRequested)
        header.addWidget(self.new_button)
        layout.addLayout(header)

        self.hint = QLabel(
            "每条清单可以单独「运行」，点哪条跑哪条 —— 不用先切过去。执行是**按顺序一条条"
            "跑完**，不是同时跑：界面自动化会抢鼠标键盘、文件流程会抢同一个文件。"
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        layout.addWidget(self.hint)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.host = QWidget()
        self.rows_layout = QVBoxLayout(self.host)
        self.rows_layout.setContentsMargins(0, 0, 8, 0)
        self.rows_layout.setSpacing(8)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(self.host)
        layout.addWidget(self.scroll, 1)

    # -- 数据 -----------------------------------------------------------------

    def set_checklists(self, items: list[tuple[str, int]], current: str) -> None:
        """``items`` 是 ``[(名字, 流程条数)]``。重建列表 —— 它很短，增量更新不划算。"""
        while self.rows_layout.count():
            entry = self.rows_layout.takeAt(0)
            widget = entry.widget() if entry is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._items.clear()

        if not items:
            empty = QLabel("还没有清单。点右上角「新建清单」建一条。")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; padding: 60px 0;")
            self.rows_layout.addWidget(empty)
        else:
            for name, count in items:
                row = ChecklistItemRow(name, count, name == current)
                row.runRequested.connect(self.runRequested)
                row.editRequested.connect(self.editRequested)
                row.renameRequested.connect(self.renameRequested)
                row.deleteRequested.connect(self.deleteRequested)
                row.settingsRequested.connect(self.settingsRequested)
                self.rows_layout.addWidget(row)
                self._items[name] = row

        self.rows_layout.addStretch(1)
        self._current = current
        self.summary.setText(f"共 {len(items)} 条清单")
        self.new_button.setEnabled(not self._running)

    def set_count(self, name: str, count: int) -> None:
        """只更新某行的条数和运行按钮 —— 不重建整行（重建会丢焦点、闪一下）。"""
        row = self._items.get(name)
        if row is not None:
            row.set_count(count)

    def checklist_row_for(self, name: str) -> ChecklistItemRow | None:
        """清单列表里的某一行。

        注意和编辑器里的 ``row_for`` 区分：那个是"清单内容里某条流程"的行。两个都叫
        ``row_for`` 的话后定义的会盖掉先定义的 —— 已经踩过一次。
        """
        return self._items.get(name)

    @property
    def current_checklist(self) -> str:
        return self._current

    # -- 运行状态 -------------------------------------------------------------

    def set_running(self, name: str | None) -> None:
        self._running = name or ""
        for row in self._items.values():
            row.setEnabled(not self._running)
        self.new_button.setEnabled(not self._running)
        self.stop_button.setEnabled(bool(self._running))

    def mark(self, name: str, kind: str, text: str) -> None:
        row = self._items.get(name)
        if row is not None:
            row.set_status(text, kind)

    def clear_marks(self) -> None:
        for row in self._items.values():
            row.set_status("", "")
