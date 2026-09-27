"""底部面板：运行日志 / 变量 / 运行历史。

这三样放在一起是因为它们回答同一个问题：**这次运行到底发生了什么。**

自动化大多是无人值守跑的，出问题时你手上只有这些痕迹。所以日志要带节点 id、
变量要实时更新、历史要落库 —— 而不是等出错才发现什么都没记。
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import theme

_LOG_LIMIT = 4000


class LogPanel(QTabWidget):
    runSelected = Signal(str)  # run_id

    def __init__(self, store: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store

        # --- 运行日志 ---
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(_LOG_LIMIT)
        self.log_view.setFrameShape(QFrame.Shape.NoFrame)
        self.log_tab_index = self.addTab(self.log_view, "运行日志")

        # --- 变量 ---
        # 空表看着和上一个页签一模一样，用户会以为"点了没反应"。所以每个空页签都给一句
        # 说明它什么时候才会有内容。
        variables_widget = QWidget()
        variables_layout = QVBoxLayout(variables_widget)
        variables_layout.setContentsMargins(6, 6, 6, 6)
        variables_layout.setSpacing(4)

        self.var_hint = QLabel("还没有变量。流程执行到 ctx.set_var() 时会实时出现在这里。")
        self.var_hint.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; padding: 6px;")
        variables_layout.addWidget(self.var_hint)

        self.var_table = QTableWidget(0, 2)
        self.var_table.setHorizontalHeaderLabels(["变量", "值"])
        self.var_table.verticalHeader().setVisible(False)
        self.var_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.var_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.var_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        variables_layout.addWidget(self.var_table, 1)
        self.addTab(variables_widget, "变量")

        # --- 运行历史 ---
        history_widget = QWidget()
        history_layout = QVBoxLayout(history_widget)
        history_layout.setContentsMargins(6, 6, 6, 6)
        history_layout.setSpacing(4)

        self.history_hint = QLabel("还没有运行记录。回到流程列表点「运行」跑一次就有了。")
        self.history_hint.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; padding: 6px;")
        history_layout.addWidget(self.history_hint)

        self.history_table = QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels(["开始时间", "工作流", "状态", "耗时", "错误"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.history_table.doubleClicked.connect(self._on_history_activated)
        history_layout.addWidget(self.history_table, 1)

        refresh = QPushButton("刷新")
        refresh.setMaximumWidth(80)
        refresh.clicked.connect(self.refresh_history)
        history_layout.addWidget(refresh)

        self.history_index = self.addTab(history_widget, "运行历史")

        self.clear_log()

    # -- 日志 -----------------------------------------------------------------

    def clear_log(self) -> None:
        self.log_view.clear()
        self.append_log("debug", "就绪。拖模块到画布，或双击左侧的模块。")

    def append_log(self, level: str, message: str, node_id: str = "") -> None:
        color = theme.LOG_LEVEL_COLORS.get(level, theme.LOG_LEVEL_COLORS["info"])
        stamp = datetime.now().strftime("%H:%M:%S")
        tag = f"[{node_id}] " if node_id else ""
        text = html.escape(f"{tag}{message}")
        self.log_view.appendHtml(
            f'<span style="color:#6b7684">{stamp}</span> '
            f'<span style="color:{color}">{text}</span>'
        )

    def show_log_tab(self) -> None:
        self.setCurrentIndex(self.log_tab_index)

    # -- 变量 -----------------------------------------------------------------

    def set_variables(self, variables: Mapping[str, Any]) -> None:
        self.var_table.setRowCount(0)
        for name in sorted(variables):
            row = self.var_table.rowCount()
            self.var_table.insertRow(row)
            self.var_table.setItem(row, 0, QTableWidgetItem(str(name)))
            self.var_table.setItem(row, 1, QTableWidgetItem(_render(variables[name])))
        self.var_hint.setVisible(self.var_table.rowCount() == 0)

    def set_variable(self, name: str, value: Any) -> None:
        for row in range(self.var_table.rowCount()):
            cell = self.var_table.item(row, 0)
            if cell is not None and cell.text() == str(name):
                self.var_table.setItem(row, 1, QTableWidgetItem(_render(value)))
                return
        row = self.var_table.rowCount()
        self.var_table.insertRow(row)
        self.var_table.setItem(row, 0, QTableWidgetItem(str(name)))
        self.var_table.setItem(row, 1, QTableWidgetItem(_render(value)))
        self.var_hint.setVisible(False)

    # -- 历史 -----------------------------------------------------------------

    def refresh_history(self) -> None:
        if self.store is None:
            self.history_hint.setText("这次运行没有开启历史记录（--no-history）。")
            self.history_hint.setVisible(True)
            return
        try:
            runs = self.store.recent_runs(100)
        except Exception:  # pragma: no cover - 历史库损坏不该拖垮界面
            return

        self.history_table.setRowCount(0)
        for run in runs:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            status = str(run.get("status") or "")
            cells = [
                str(run.get("started_at") or "").replace("T", " "),
                str(run.get("workflow_name") or ""),
                _STATUS_TEXT.get(status, status),
                f"{(run.get('duration_ms') or 0) / 1000:.2f}s",
                str(run.get("error") or ""),
            ]
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if column == 2:
                    cell.setForeground(QColor(_STATUS_COLORS.get(status, theme.NODE_TEXT)))
                cell.setData(Qt.ItemDataRole.UserRole, run.get("run_id"))
                self.history_table.setItem(row, column, cell)

        if runs:
            self.history_hint.setVisible(False)
        else:
            self.history_hint.setText("还没有运行记录。回到流程列表点「运行」跑一次就有了。")
            self.history_hint.setVisible(True)

    def _on_history_activated(self, index: Any) -> None:
        cell = self.history_table.item(index.row(), 0)
        if cell is not None:
            run_id = cell.data(Qt.ItemDataRole.UserRole)
            if run_id:
                self.runSelected.emit(str(run_id))


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


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    import json

    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
