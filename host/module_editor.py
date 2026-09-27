"""模块代码编辑器。

点「新建模块」**直接开到这里**，不弹对话框 —— 用户要的就是一个能写代码的地方，中间加一层
"先填四个字段、再点确定、然后才给代码框"只是多两步。

保存时做三件事，缺一不可：

1. **标识合法性**（新建时）：格式、以及别跟已有模块撞。撞了会把人家的代码冲掉。
2. **``ast.parse`` 一遍**。语法错误的插件一旦落盘，整个注册表会把它标成"加载失败"，
   而失败现场是"我明明写好了" —— 在这里拦住，错误信息还能指到具体行列。
3. **目录名和标识保持一致**。内核靠这个找插件。

改已有模块时走另一条路（``Registry.save_source``）：只覆盖 ``main.py`` 和那几个 manifest
字段，不整份重写 —— 用户自己往 manifest 里加的字段不该被无声抹掉。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QTextCursor, QTextFormat
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from kernel.authoring import PLUGIN_ID_PATTERN, PluginDraft, plugin_template

from . import theme

__all__ = ["ModuleEditorView"]

_SAMPLE = plugin_template(
    plugin_id="user.my_module", name="我的模块", description="写一句它是干什么的", category="自定义"
)


class _LineNumbers(QWidget):
    """行号槽。没有它，''语法错误：第 37 行'' 这句话就没法用。"""

    def __init__(self, editor: "_CodeEditor") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 重写
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        self.editor.paint_line_numbers(event)


class _CodeEditor(QPlainTextEdit):
    """等宽、Tab 转 4 空格、带行号和错误行高亮。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        font = QFont()
        font.setFamilies(["Cascadia Mono", "Consolas", "Courier New", "monospace"])
        font.setFixedPitch(True)
        font.setPointSizeF(9.5)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self._error_line = 0
        self._numbers = _LineNumbers(self)
        self.blockCountChanged.connect(lambda _: self._refresh_numbers())
        self.updateRequest.connect(self._on_update_request)
        self._refresh_numbers()

    # -- 行号 -----------------------------------------------------------------

    def line_number_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def _refresh_numbers(self) -> None:
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _on_update_request(self, rect: QRect, dy: int) -> None:
        if dy:
            self._numbers.scroll(0, dy)
        else:
            self._numbers.update(0, rect.y(), self._numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._refresh_numbers()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        super().resizeEvent(event)
        box = self.contentsRect()
        self._numbers.setGeometry(
            QRect(box.left(), box.top(), self.line_number_width(), box.height())
        )

    def paint_line_numbers(self, event: Any) -> None:
        painter = QPainter(self._numbers)
        painter.fillRect(event.rect(), QColor(theme.PANEL_BG))

        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(
            self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        )
        bottom = top + round(self.blockBoundingRect(block).height())
        current = self.textCursor().blockNumber()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                if number == self._error_line:
                    painter.setPen(QColor(theme.STATE_FAILED))
                elif number == current:
                    painter.setPen(QColor(theme.NODE_TEXT))
                else:
                    painter.setPen(QColor(theme.NODE_SUBTITLE))
                painter.drawText(
                    0,
                    top,
                    self._numbers.width() - 6,
                    self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight,
                    str(number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1

    # -- 行为 -----------------------------------------------------------------

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        # Tab 应该缩进，不是把焦点跳走 —— 在一个代码框里按 Tab 跳到别处很反直觉。
        if event.key() == Qt.Key.Key_Tab and not event.modifiers():
            self.insertPlainText("    ")
            return
        super().keyPressEvent(event)

    def set_error_line(self, line: int) -> None:
        self._error_line = line
        selections: list[QTextEdit.ExtraSelection] = []
        if line > 0:
            block = self.document().findBlockByNumber(line - 1)
            if block.isValid():
                selection = QTextEdit.ExtraSelection()
                selection.format.setBackground(QColor(90, 32, 36))
                selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
                cursor = QTextCursor(block)
                selection.cursor = cursor
                selections.append(selection)
        self.setExtraSelections(selections)
        self._numbers.update()


class ModuleEditorView(QWidget):
    """右侧那一整块：字段 + 代码 + 校验状态。"""

    saved = Signal(str)  # 保存成功后的 plugin_id
    failed = Signal(str)  # 写盘失败的原因
    cancelled = Signal()

    def __init__(self, registry: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.registry = registry
        self._editing: str | None = None  # 正在改的已有模块 id；None 表示新建

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)
        self.heading = QLabel("新建模块")
        heading_font = QFont()
        heading_font.setPointSizeF(14.0)
        heading_font.setBold(True)
        self.heading.setFont(heading_font)
        header.addWidget(self.heading)

        self.subtitle = QLabel("在下面写 main.py，保存后模块会出现在左侧栏里")
        self.subtitle.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        header.addWidget(self.subtitle)
        header.addStretch(1)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.cancelled)
        header.addWidget(self.cancel_button)

        self.save_button = QPushButton("保存模块")
        self.save_button.setStyleSheet(
            f"QPushButton {{ background: {theme.STATE_SUCCESS}; color: #12281a;"
            " font-weight: bold; border: none; border-radius: 6px; padding: 7px 18px; }"
            "QPushButton:hover { background: #5fc074; }"
            "QPushButton:disabled { background: #3a4149; color: #6b7684; }"
        )
        self.save_button.clicked.connect(self.save)
        header.addWidget(self.save_button)
        layout.addLayout(header)

        # --- 字段 ---
        fields = QFrame()
        fields.setObjectName("moduleFields")
        fields.setStyleSheet(
            f"QFrame#moduleFields {{ background: {theme.NODE_BODY};"
            f" border: 1px solid {theme.NODE_BORDER}; border-radius: 8px; }}"
        )
        form = QFormLayout(fields)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(7)

        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("user.my_module")
        self.id_edit.textChanged.connect(self._revalidate)
        form.addRow("标识", self.id_edit)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("我的模块")
        self.name_edit.textChanged.connect(self._revalidate)
        form.addRow("名称", self.name_edit)

        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("一句话说明它是干什么的")
        form.addRow("描述", self.description_edit)

        self.category_edit = QLineEdit("自定义")
        form.addRow("分类", self.category_edit)

        layout.addWidget(fields)

        # --- 代码 ---
        code_header = QHBoxLayout()
        code_header.setSpacing(8)
        code_label = QLabel("main.py")
        code_label.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        code_header.addWidget(code_label)
        code_header.addStretch(1)
        self.syntax = QLabel()
        self.syntax.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        code_header.addWidget(self.syntax)
        layout.addLayout(code_header)

        self.code = _CodeEditor()
        self.code.setPlainText(_SAMPLE)
        self.code.textChanged.connect(self._schedule_check)
        layout.addWidget(self.code, 1)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 10px;")
        layout.addWidget(self.hint)

        # 语法检查要防抖：每敲一个字符都 parse 一次，大文件会卡手。
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(220)
        self._check_timer.timeout.connect(self._revalidate)

        self.start_new()

    # -- 进入方式 -------------------------------------------------------------

    def start_new(self, suggested_id: str = "user.my_module") -> None:
        self._editing = None
        self.heading.setText("新建模块")
        self.subtitle.setText("在下面写 main.py，保存后模块会出现在左侧栏里")
        self.id_edit.setReadOnly(False)
        self.id_edit.setText(suggested_id)
        self.name_edit.setText("我的模块")
        self.description_edit.clear()
        self.category_edit.setText("自定义")
        self.code.setPlainText(_SAMPLE)
        self.hint.setText(
            "保存时会先检查标识是否可用、代码有没有语法错误 —— 有问题的插件一旦落盘，"
            "整个注册表都会把它标成加载失败。"
        )
        self._revalidate()

    def load(self, plugin_id: str) -> bool:
        """打开一个已有模块改代码。内置模块不给改。"""
        if self.registry.is_builtin(plugin_id):
            return False
        manifest = self.registry.manifests.get(plugin_id)
        if manifest is None:
            return False

        entry = Path(manifest.path) / manifest.runtime.entry
        try:
            code = entry.read_text(encoding="utf-8")
        except OSError:
            code = ""

        self._editing = plugin_id
        self.heading.setText(f"编辑模块 · {manifest.name}")
        self.subtitle.setText("标识和目录不能改（改了引用它的流程会全部失效）")
        self.id_edit.setText(plugin_id)
        self.id_edit.setReadOnly(True)
        self.name_edit.setText(manifest.name)
        self.description_edit.setText(manifest.description)
        self.category_edit.setText(manifest.category)
        self.code.setPlainText(code)
        self.hint.setText(f"文件：{entry}")
        self._revalidate()
        return True

    @property
    def editing(self) -> str | None:
        return self._editing

    # -- 校验 -----------------------------------------------------------------

    def problems(self) -> list[str]:
        issues: list[str] = []
        plugin_id = self.id_edit.text().strip()

        if not plugin_id:
            issues.append("标识不能为空")
        elif not PLUGIN_ID_PATTERN.match(plugin_id):
            issues.append("标识只能有字母、数字、点、下划线、连字符，且以字母或数字开头")
        elif self._editing is None and plugin_id in self.registry.manifests:
            issues.append(f"已经有一个模块叫 {plugin_id} 了")
        elif self._editing is not None and plugin_id != self._editing:
            issues.append("标识不能改")

        if not self.name_edit.text().strip():
            issues.append("名称不能为空")

        source = self.code.toPlainText()
        if not source.strip():
            issues.append("代码不能为空")
        else:
            try:
                ast.parse(source)
            except SyntaxError as exc:
                issues.append(f"语法错误：第 {exc.lineno} 行 {exc.msg}")
        return issues

    def _schedule_check(self) -> None:
        self._check_timer.start()

    def _revalidate(self) -> None:
        issues = self.problems()
        if issues:
            self.syntax.setText("· " + "；".join(issues))
            self.syntax.setStyleSheet(f"color: {theme.STATE_FAILED}; font-size: 11px;")
            self.save_button.setEnabled(False)
        else:
            self.syntax.setText("语法 OK，可以保存")
            self.syntax.setStyleSheet(f"color: {theme.STATE_SUCCESS}; font-size: 11px;")
            self.save_button.setEnabled(True)

        # 语法错误时把那行highlight出来 —— 光说"第 N 行"还要人去数。
        line = 0
        try:
            ast.parse(self.code.toPlainText())
        except SyntaxError as exc:
            line = int(exc.lineno or 0)
        self.code.set_error_line(line)

    # -- 保存 -----------------------------------------------------------------

    def save(self) -> None:
        from kernel.errors import MyAutoWorkError  # noqa: PLC0415

        issues = self.problems()
        if issues:
            self._revalidate()
            return

        plugin_id = self.id_edit.text().strip()
        code = self.code.toPlainText()
        name = self.name_edit.text().strip()
        description = self.description_edit.text().strip()
        category = self.category_edit.text().strip() or "自定义"

        try:
            if self._editing is None:
                PluginDraft(
                    plugin_id=plugin_id,
                    name=name,
                    description=description,
                    category=category,
                    author="我自己",
                    keywords=[category],
                    code=code,
                    provides={"actions": self._declared_actions(code)},
                    origin="手动编写",
                ).write(self.registry.plugins_dir)
            else:
                self.registry.save_source(
                    self._editing,
                    code=code,
                    name=name,
                    description=description,
                    category=category,
                )
        except (MyAutoWorkError, OSError) as exc:
            # 只管把原因报出去；界面状态保持原样，用户写的代码一个字都不动。
            self.failed.emit(str(exc))
            return

        self.saved.emit(plugin_id)

    @staticmethod
    def _declared_actions(code: str) -> list[str]:
        """从代码里认出注册了哪些动作 id。

        只用于 ``provides`` 的静态声明；真正的接口以进程加载结果为准（内核会交叉校验）。
        认不出来就给一个 ``run``，反正对不上时内核会报"manifest 与代码不一致"而不是
        静默跑错。
        """
        found: list[str] = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return ["run"]
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                for keyword in decorator.keywords:
                    if keyword.arg == "id" and isinstance(keyword.value, ast.Constant):
                        found.append(str(keyword.value.value))
        return found or ["run"]
