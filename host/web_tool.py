"""网页工具窗口：实时显示你在页面上点了什么。

**它和 UIA 拾取器是并列的两个工具** —— 那个抓 Windows 界面元素，这个抓网页元素。

**数据是宿主轮询来的，不是插件推的。** 插件进程只在被调用时才干活，它没有后台
循环，所以"插件主动推事件给界面"这条路现在走不通（长驻触发器那套还没做）。
这里的做法是：一个 QTimer 每 400 毫秒调一次插件的「取回录制」，把新增的步骤
追加到列表里。够实时了，而且不动插件一行代码。

**这也是宿主第一次直接调插件动作。** 在那之前，所有插件调用都发生在"运行"里
（走引擎）。这个窗口是工具，没有运行上下文，所以自己拼 invoke 请求。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kernel.errors import MyAutoWorkError

from . import theme

__all__ = ["WebToolWindow"]

PLUGIN_ID = "web.browser"

#: 多久问一次插件。太快没必要（人在操作，不是机器在刷），太慢就不"实时"了。
POLL_MS = 400


class WebToolWindow(QDialog):
    """网页操作监视器。"""

    def __init__(self, registry: Any, workdir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.registry = registry
        self.workdir = Path(workdir)
        self._seen = 0
        self._recording = False

        self.setWindowTitle("网页工具")
        self.resize(880, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        bar = QHBoxLayout()
        self.start_button = QPushButton("开始录制")
        self.start_button.clicked.connect(self.start_recording)
        bar.addWidget(self.start_button)

        self.stop_button = QPushButton("停止录制")
        self.stop_button.clicked.connect(self.stop_recording)
        self.stop_button.setEnabled(False)
        bar.addWidget(self.stop_button)

        self.clear_button = QPushButton("清空列表")
        self.clear_button.clicked.connect(self.clear_list)
        bar.addWidget(self.clear_button)

        bar.addStretch(1)
        self.status = QLabel("还没开始。点「开始录制」，然后去浏览器里正常操作")
        self.status.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        bar.addWidget(self.status)
        layout.addLayout(bar)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "动作", "选择器", "备注"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        # 双击一行就把选择器复制走 —— 拿到之后下一步就是把它填进节点里，
        # 这一步手工选中一长串 CSS 很烦。
        self.table.cellDoubleClicked.connect(self._copy_row)
        layout.addWidget(self.table, 1)

        hint = QLabel(
            "双击一行可以复制选择器。"
            "**同站内跳转能连续录；跳到别的网站会丢掉之前录的**（记录按站点隔离）。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        layout.addWidget(hint)

        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)

    # -- 调插件 ---------------------------------------------------------------

    def _invoke(self, action_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """直接调插件的一个动作。

        这是宿主第一次这么做 —— 以前所有插件调用都在"运行"里，由引擎发起。
        工具窗口没有运行上下文，所以自己拼一份 invoke 请求。
        """
        if not self.registry.is_builtin(PLUGIN_ID) and PLUGIN_ID not in {
            p["id"] for p in self.registry.catalog()
        }:
            raise MyAutoWorkError(f"没装 {PLUGIN_ID} 这个插件")

        client = self.registry.client(PLUGIN_ID)
        response = client.request(
            "invoke",
            {
                "action_id": action_id,
                "node_id": "web-tool",
                "run_id": "web-tool",
                "params": params,
                "variables": {},
                "workdir": str(self.workdir),
                "artifacts_dir": str(self.workdir / "artifacts"),
            },
            timeout=30.0,
        )
        return dict(response.get("output") or {})

    def _fail(self, title: str, exc: Exception) -> None:
        self.status.setText(f"{title}：{exc}")
        self.status.setStyleSheet(f"color: {theme.STATE_FAILED};")

    # -- 按钮 -----------------------------------------------------------------

    def start_recording(self) -> None:
        try:
            self._invoke("record_start", {})
        except Exception as exc:  # noqa: BLE001 - 什么错都可能，都要显示出来
            self._fail("开不了录制", exc)
            QMessageBox.warning(self, "网页工具", f"开不了录制：\n\n{exc}")
            return
        self._recording = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status.setStyleSheet(f"color: {theme.STATE_SUCCESS};")
        self.status.setText("正在录制 —— 去浏览器里正常操作")
        self.timer.start()

    def stop_recording(self) -> None:
        self.timer.stop()
        self._recording = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        try:
            self.poll()  # 停之前把最后几步捞回来
            self._invoke("record_stop", {})
        except Exception as exc:  # noqa: BLE001
            self._fail("停不下来", exc)
            return
        self.status.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        self.status.setText(f"已停止，共 {self.table.rowCount()} 步。双击一行可以复制选择器")

    def clear_list(self) -> None:
        self.table.setRowCount(0)
        self._seen = 0

    # -- 轮询 -----------------------------------------------------------------

    def poll(self) -> None:
        try:
            output = self._invoke("record_dump", {"clear": False})
        except Exception as exc:  # noqa: BLE001
            self._fail("取不到录制", exc)
            return

        try:
            rows = json.loads(str(output.get("json") or "[]"))
        except Exception:
            rows = []

        # **只追加新的。** 整个重建的话，用户正在看的选中行会跳走，
        # 而且表格会闪。
        for row in rows[self._seen :]:
            self._append(row)
        self._seen = len(rows)

    def _append(self, row: dict[str, Any]) -> None:
        kind = str(row.get("type") or "")
        label = {"click": "点击", "input": "输入"}.get(kind, kind)
        note = row.get("value") if kind == "input" else row.get("text")
        if kind == "input":
            note = f"= {note!r}" if note is not None else ""

        index = self.table.rowCount() + 1
        self.table.insertRow(self.table.rowCount())
        for column, text in enumerate(
            [str(index), label, str(row.get("selector") or ""), str(note or "")]
        ):
            item = QTableWidgetItem(text)
            if column == 0:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(self.table.rowCount() - 1, column, item)
        self.table.scrollToBottom()

    def _copy_row(self, row: int, _column: int) -> None:
        item = self.table.item(row, 2)
        if item is None or not item.text():
            return
        QGuiApplication.clipboard().setText(item.text())
        self.status.setStyleSheet(f"color: {theme.STATE_SUCCESS};")
        self.status.setText(f"已复制选择器：{item.text()}")

    # -- 收尾 -----------------------------------------------------------------

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        self.timer.stop()
        if self._recording:
            # **关窗口时要把录制停掉。** 不停的话录制脚本还挂在页面上，
            # 用户以为已经结束了，实际上还在往 sessionStorage 里堆东西。
            try:
                self._invoke("record_stop", {})
            except Exception:
                pass
        super().closeEvent(event)
