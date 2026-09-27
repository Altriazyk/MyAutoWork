"""schema → Qt 控件。

这是全项目复用率最高的一块：**插件加多少，这里一行都不用改。**

    Field("string")            → QLineEdit
    Field("text")              → 多行 QPlainTextEdit
    Field("number")            → QDoubleSpinBox（带 min/max）
    Field("bool")              → QCheckBox
    Field("enum")              → QComboBox（choices 直接变选项）
    Field("file"/"folder")     → QLineEdit + 浏览按钮
    Field("element")           → QLineEdit + 🎯 拾取按钮
    Field("code")              → 等宽多行编辑器
    Field("credential")        → 密码框
    Field("list"/"dict"/"any") → JSON 编辑器

每个参数行还带一个"取值来源"下拉（常量 / 表达式 / 连线），这是双线模型在界面上的
落点 —— 用户点一下就能把写死的路径换成上一步的输出，不用改工作流结构。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from kernel.graph import MODE_EXPR, MODE_LITERAL, MODE_UPSTREAM, ParamValue

from .. import theme

# --------------------------------------------------------------------------- #
# 纯值编辑器
# --------------------------------------------------------------------------- #


class ValueEditor(QWidget):
    """只负责"值"，不管取值来源。"""

    changed = Signal()

    def value(self) -> Any:  # pragma: no cover - 抽象
        raise NotImplementedError

    def set_value(self, value: Any) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError


class _LineEdit(ValueEditor):
    def __init__(self, parent: QWidget | None = None, *, password: bool = False) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.edit = QLineEdit()
        if password:
            self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.textChanged.connect(self.changed)
        layout.addWidget(self.edit, 1)

    def value(self) -> Any:
        return self.edit.text()

    def set_value(self, value: Any) -> None:
        self.edit.setText("" if value is None else str(value))

    def set_placeholder(self, text: str) -> None:
        self.edit.setPlaceholderText(text)


class _MultiLineEdit(ValueEditor):
    def __init__(self, parent: QWidget | None = None, *, monospace: bool = False) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QPlainTextEdit()
        self.edit.setFixedHeight(84)
        self.edit.setTabChangesFocus(True)
        if monospace:
            font = self.edit.font()
            font.setFamilies(["Cascadia Mono", "Consolas", "Courier New", "monospace"])
            self.edit.setFont(font)
        self.edit.textChanged.connect(self.changed)
        layout.addWidget(self.edit)

    def value(self) -> Any:
        return self.edit.toPlainText()

    def set_value(self, value: Any) -> None:
        self.edit.setPlainText("" if value is None else str(value))


class _NumberEdit(ValueEditor):
    def __init__(self, schema: Mapping[str, Any], parent: QWidget | None = None, *, integer: bool) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if integer:
            self.spin: QSpinBox | QDoubleSpinBox = QSpinBox()
            self.spin.setRange(
                int(schema.get("minimum", -2_147_483_648)),
                int(schema.get("maximum", 2_147_483_647)),
            )
        else:
            self.spin = QDoubleSpinBox()
            self.spin.setDecimals(4)
            self.spin.setRange(
                float(schema.get("minimum", -1e12)),
                float(schema.get("maximum", 1e12)),
            )
        self.spin.setKeyboardTracking(False)  # 边打字边触发会把手输的中间态写进模型
        self.spin.valueChanged.connect(self.changed)
        layout.addWidget(self.spin, 1)

    def value(self) -> Any:
        return self.spin.value()

    def set_value(self, value: Any) -> None:
        try:
            self.spin.setValue(0 if value is None else type(self.spin.value())(value))
        except (TypeError, ValueError):
            self.spin.setValue(0)


class _BoolEdit(ValueEditor):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.box = QCheckBox()
        self.box.toggled.connect(self.changed)
        layout.addWidget(self.box)
        layout.addStretch(1)

    def value(self) -> Any:
        return self.box.isChecked()

    def set_value(self, value: Any) -> None:
        self.box.setChecked(bool(value))


class _EnumEdit(ValueEditor):
    def __init__(self, schema: Mapping[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        for choice in schema.get("choices") or []:
            self.combo.addItem(str(choice), choice)
        self.combo.currentIndexChanged.connect(self.changed)
        layout.addWidget(self.combo, 1)

    def value(self) -> Any:
        return self.combo.currentData()

    def set_value(self, value: Any) -> None:
        index = self.combo.findData(value)
        if index < 0:
            index = self.combo.findText(str(value))
        if index >= 0:
            self.combo.setCurrentIndex(index)


class _PathEdit(ValueEditor):
    """文件 / 目录。带浏览按钮 —— 这是 ``Field(picker=...)`` 在界面上的落点。"""

    def __init__(self, schema: Mapping[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.schema = schema
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.edit = QLineEdit()
        self.edit.textChanged.connect(self.changed)
        self.button = QPushButton("浏览")
        self.button.setMaximumWidth(56)
        self.button.clicked.connect(self._browse)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)

    def _browse(self) -> None:
        picker = self.schema.get("picker") or "open"
        if picker == "folder":
            path = QFileDialog.getExistingDirectory(self, "选择目录", self.edit.text() or ".")
        elif picker == "save":
            path, _ = QFileDialog.getSaveFileName(self, "选择保存位置", self.edit.text() or "")
        else:
            path, _ = QFileDialog.getOpenFileName(self, "选择文件", self.edit.text() or "")
        if path:
            self.edit.setText(path)

    def value(self) -> Any:
        return self.edit.text()

    def set_value(self, value: Any) -> None:
        self.edit.setText("" if value is None else str(value))


class _ElementEdit(ValueEditor):
    """界面元素定位器。

    🎯 按钮的**位置**在这一步就占好了：``Element(picker="uia")`` 声明的字段会自动长出
    这个按钮，这验证了"参数类型决定界面控件"这条机制。真正的瞄准镜拾取器属于第 4 步
    （界面自动化），届时只要把 ``pickRequested`` 接到拾取器上，这里不用改。
    """

    pickRequested = Signal(str)

    def __init__(self, port: str, schema: Mapping[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.port = port
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("AutomationId / Name / 路径")
        self.edit.textChanged.connect(self.changed)

        self.button = QPushButton("🎯")
        self.button.setToolTip("从屏幕上拾取元素（第 4 步实现）")
        self.button.setMaximumWidth(34)
        self.button.clicked.connect(lambda: self.pickRequested.emit(self.port))

        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)

    def value(self) -> Any:
        text = self.edit.text().strip()
        if not text:
            return None
        # 允许直接粘贴 JSON 选择器；否则当作 AutomationId 简写。
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return {"automation_id": text}

    def set_value(self, value: Any) -> None:
        if value is None:
            self.edit.clear()
        elif isinstance(value, str):
            self.edit.setText(value)
        elif isinstance(value, Mapping) and set(value) == {"automation_id"}:
            self.edit.setText(str(value["automation_id"]))
        else:
            self.edit.setText(json.dumps(value, ensure_ascii=False))


class _JsonEdit(ValueEditor):
    """list / dict / any 用 JSON 文本编辑，非法时把边框标红而不是静默吞掉。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QPlainTextEdit()
        self.edit.setFixedHeight(72)
        self.edit.setTabChangesFocus(True)
        self.edit.textChanged.connect(self._on_text)
        layout.addWidget(self.edit)
        self._valid = True

    def _on_text(self) -> None:
        text = self.edit.toPlainText().strip()
        valid = True
        if text:
            try:
                json.loads(text)
            except json.JSONDecodeError:
                valid = False
        if valid != self._valid:
            self._valid = valid
            self.edit.setStyleSheet("" if valid else f"border: 1px solid {theme.STATE_FAILED};")
        self.changed.emit()

    def value(self) -> Any:
        text = self.edit.toPlainText().strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 保持原样传下去，让运行时的报错指出真正的问题，而不是在这里悄悄改数据。
            return text

    def set_value(self, value: Any) -> None:
        if value is None:
            self.edit.setPlainText("")
        elif isinstance(value, str):
            self.edit.setPlainText(value)
        else:
            self.edit.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))


def create_value_editor(port: str, schema: Mapping[str, Any]) -> ValueEditor:
    """按字段声明挑控件。这就是整个表单生成器的全部逻辑。"""
    kind = schema.get("kind", "any")
    if kind == "string":
        return _LineEdit()
    if kind == "text":
        return _MultiLineEdit()
    if kind == "code":
        return _MultiLineEdit(monospace=True)
    if kind == "number":
        return _NumberEdit(schema, integer=False)
    if kind == "integer":
        return _NumberEdit(schema, integer=True)
    if kind == "bool":
        return _BoolEdit()
    if kind == "enum":
        return _EnumEdit(schema)
    if kind in ("file", "folder"):
        return _PathEdit(schema)
    if kind == "element":
        return _ElementEdit(port, schema)
    if kind == "credential":
        return _LineEdit(password=True)
    if kind == "color":
        return _LineEdit()
    return _JsonEdit()


# --------------------------------------------------------------------------- #
# 参数行
# --------------------------------------------------------------------------- #

_MODE_LABELS = [
    (MODE_LITERAL, "常量"),
    (MODE_EXPR, "表达式"),
    (MODE_UPSTREAM, "连线"),
]


class ParamRow(QFrame):
    """一个参数：标签 + 取值来源 + 编辑控件 + 帮助文本。"""

    changed = Signal()
    pickRequested = Signal(str)

    def __init__(
        self,
        port: str,
        schema: Mapping[str, Any],
        *,
        upstream_from: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.port = port
        self.schema = dict(schema)
        self.upstream_from = upstream_from

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(3)

        # --- 第一行：名字 + 取值来源 ---
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        label_text = str(schema.get("label") or port)
        if schema.get("required"):
            label_text += " *"
        label = QLabel(label_text)
        label.setToolTip(f"{port} · {schema.get('kind', 'any')}")
        head.addWidget(label)
        head.addStretch(1)

        self.mode_combo = QComboBox()
        self.mode_combo.setMaximumWidth(84)
        for mode, text in _MODE_LABELS:
            self.mode_combo.addItem(text, mode)
        if upstream_from is None:
            # 没有连线时"连线"模式不可选，避免选出一个取不到值的东西。
            item_index = self.mode_combo.findData(MODE_UPSTREAM)
            model_item = self.mode_combo.model().item(item_index)
            if model_item is not None:
                model_item.setEnabled(False)
            self.mode_combo.setItemData(
                item_index, "该输入还没有连线，无法使用上游取值", Qt.ItemDataRole.ToolTipRole
            )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        head.addWidget(self.mode_combo)
        layout.addLayout(head)

        # --- 第二行：值 ---
        self.stack = QStackedWidget()
        self.value_editor = create_value_editor(port, self.schema)
        self.value_editor.changed.connect(self.changed)
        if isinstance(self.value_editor, _ElementEdit):
            self.value_editor.pickRequested.connect(self.pickRequested)
        self.stack.addWidget(self.value_editor)  # index 0: literal

        self.expr_edit = QLineEdit()
        self.expr_edit.setPlaceholderText("{{ $node.n2.text }} / {{ $date }}")
        self.expr_edit.textChanged.connect(self.changed)
        self.stack.addWidget(self.expr_edit)  # index 1: expr

        self.upstream_label = QLabel()
        self.upstream_label.setWordWrap(True)
        self.stack.addWidget(self.upstream_label)  # index 2: upstream

        layout.addWidget(self.stack)

        # --- 第三行：帮助 ---
        help_text = schema.get("help")
        if help_text:
            hint = QLabel(str(help_text))
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
            layout.addWidget(hint)

        self._apply_mode(MODE_LITERAL)
        if schema.get("default") is not None:
            self.value_editor.set_value(schema["default"])

    # -- 模式 ----------------------------------------------------------------

    def _current_mode(self) -> str:
        return self.mode_combo.currentData() or MODE_LITERAL

    def _on_mode_changed(self) -> None:
        self._apply_mode(self._current_mode())
        self.changed.emit()

    def _apply_mode(self, mode: str) -> None:
        if mode == MODE_EXPR:
            self.stack.setCurrentIndex(1)
        elif mode == MODE_UPSTREAM:
            self.stack.setCurrentIndex(2)
            self.upstream_label.setText(
                f"来自连线：{self.upstream_from}" if self.upstream_from else "尚未连线"
            )
            self.upstream_label.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        else:
            self.stack.setCurrentIndex(0)

    # -- 取值 ----------------------------------------------------------------

    def to_param(self) -> ParamValue:
        mode = self._current_mode()
        if mode == MODE_EXPR:
            return ParamValue(mode=MODE_EXPR, value=self.expr_edit.text())
        if mode == MODE_UPSTREAM:
            return ParamValue(mode=MODE_UPSTREAM)
        return ParamValue(mode=MODE_LITERAL, value=self.value_editor.value())

    def from_param(self, param: ParamValue | None) -> None:
        if param is None:
            self._select_mode(MODE_LITERAL)
            return
        if param.mode == MODE_EXPR:
            self.expr_edit.setText("" if param.value is None else str(param.value))
            self._select_mode(MODE_EXPR)
        elif param.mode == MODE_UPSTREAM:
            self._select_mode(MODE_UPSTREAM)
        else:
            self.value_editor.set_value(param.value)
            self._select_mode(MODE_LITERAL)

    def _select_mode(self, mode: str) -> None:
        index = self.mode_combo.findData(mode)
        if index >= 0:
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(index)
            self.mode_combo.blockSignals(False)
        self._apply_mode(mode)

    def set_upstream(self, description: str | None) -> None:
        self.upstream_from = description
        index = self.mode_combo.findData(MODE_UPSTREAM)
        model_item = self.mode_combo.model().item(index)
        if model_item is not None:
            model_item.setEnabled(description is not None)
        if description is None and self._current_mode() == MODE_UPSTREAM:
            self._select_mode(MODE_LITERAL)
        self._apply_mode(self._current_mode())


# --------------------------------------------------------------------------- #
# 参数表单
# --------------------------------------------------------------------------- #


class ParamForm(QWidget):
    """一个节点的全部输入参数。"""

    changed = Signal()
    pickRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(6)
        self.rows: dict[str, ParamRow] = {}
        # 构建期间必须屏蔽 changed：给控件 set_value 会触发 textChanged，
        # 而那时 rows 还只建了一半 —— 外面若在这时候把表单回写到模型，
        # 节点参数会被覆盖成残缺的字典（曾经真的把必填参数整个弄丢）。
        self._building = False
        self._header = QLabel("未选择节点")
        self._header.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        self._header.setWordWrap(True)
        self._layout.addWidget(self._header)
        self._layout.addStretch(1)

    def build(
        self,
        inputs: Mapping[str, Mapping[str, Any]],
        params: Mapping[str, ParamValue],
        upstream: Mapping[str, str],
        *,
        header: str = "",
        outputs: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """按接口声明重建整个表单。构建期间不会发 changed。"""
        self._building = True
        try:
            self._build_inner(inputs, params, upstream, header=header, outputs=outputs)
        finally:
            self._building = False

    def _build_inner(
        self,
        inputs: Mapping[str, Mapping[str, Any]],
        params: Mapping[str, ParamValue],
        upstream: Mapping[str, str],
        *,
        header: str = "",
        outputs: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._clear()
        self._header.setText(header)

        if not inputs:
            empty = QLabel("这个动作没有输入参数。")
            empty.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
            self._layout.insertWidget(1, empty)
            self._add_outputs(outputs)
            return

        for port, schema in inputs.items():
            row = ParamRow(port, schema, upstream_from=upstream.get(port))
            row.changed.connect(self._on_row_changed)
            row.pickRequested.connect(self.pickRequested)
            row.from_param(params.get(port))
            self.rows[port] = row
            self._layout.insertWidget(self._layout.count() - 1, row)

        self._add_outputs(outputs)

    def _on_row_changed(self) -> None:
        if self._building:
            return
        self.changed.emit()

    def _add_outputs(self, outputs: Mapping[str, Mapping[str, Any]] | None) -> None:
        if not outputs:
            return
        title = QLabel("输出")
        title.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; margin-top: 8px;")
        self._layout.insertWidget(self._layout.count() - 1, title)
        for port, schema in outputs.items():
            kind = schema.get("kind", "any")
            item = QLabel(f"• {schema.get('label') or port}  ({kind})")
            item.setStyleSheet(f"color: {theme.port_color('data', kind)};")
            self._layout.insertWidget(self._layout.count() - 1, item)

    def params(self) -> dict[str, ParamValue]:
        return {port: row.to_param() for port, row in self.rows.items()}

    def set_upstream(self, port: str, description: str | None) -> None:
        row = self.rows.get(port)
        if row is not None:
            row.set_upstream(description)

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.rows.clear()
        # 表头被一起清掉了，重建一个。
        self._header = QLabel("")
        self._header.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        self._header.setWordWrap(True)
        self._layout.addWidget(self._header)
        self._layout.addStretch(1)
