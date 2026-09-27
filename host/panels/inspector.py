"""右侧属性面板。

它做的事只有一件：把选中节点的**接口声明**交给表单生成器。所以：

- 选中一个"写文件"节点 → 自动出现路径框 + 浏览按钮、多行内容框、编码下拉
- 选中一个带 ``Element`` 参数的节点 → 自动出现 🎯 拾取按钮
- 选中一个没有输入的节点 → 显示"这个动作没有输入参数"

面板里没有一行代码认识具体插件。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..forms.builder import ParamForm


def _pretty(value: Any, *, limit: int = 400) -> str:
    """把节点输出显示成人看的样子。

    **字符串不加引号** —— 输出多半本来就是文本，加一圈引号只是噪音。
    太长的截断，并明说截了多少 —— 悄悄截断会让人以为"它就这么多"。
    """
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list, tuple)):
        try:
            import json  # noqa: PLC0415

            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # pragma: no cover - 理论上不会走到
            text = str(value)
    else:
        text = repr(value)

    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（还有 {len(text) - limit} 个字）"


def _separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"color: {theme.PANEL_BORDER};")
    return line


class InspectorPanel(QWidget):
    paramChanged = Signal()
    pickRequested = Signal(str)

    def __init__(self, scene: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scene = scene
        self._current: Any = None
        self._updating = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.header = QLabel("未选择节点")
        self.header.setWordWrap(True)
        self.header.setStyleSheet(f"color: {theme.NODE_TEXT}; font-weight: bold;")
        layout.addWidget(self.header)

        self.subheader = QLabel("在画布上点一个节点")
        self.subheader.setWordWrap(True)
        self.subheader.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        layout.addWidget(self.subheader)

        layout.addWidget(_separator())

        self.form = ParamForm()
        self.form.changed.connect(self._on_form_changed)
        self.form.pickRequested.connect(self.pickRequested)
        layout.addWidget(self.form)

        # 「上次输出」摆在参数**下面**。排错时的动作顺序就是"我填的参数对吗" →
        # "那它到底算出了什么"，两件事挨着最顺。没跑过的时候整块藏起来 ——
        # 常驻一个空框只会占地方。
        self.output_title = QLabel("上次输出")
        self.output_title.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        layout.addWidget(self.output_title)
        self.output_view = QPlainTextEdit()
        self.output_view.setReadOnly(True)
        self.output_view.setMinimumHeight(70)
        self.output_view.setMaximumHeight(170)
        # 等宽字体：输出多半是路径、数字、JSON，对齐了才看得清。
        self.output_view.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        layout.addWidget(self.output_view)
        self._set_output_visible(False)

        layout.addWidget(_separator())

        settings_title = QLabel("节点设置")
        settings_title.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        layout.addWidget(settings_title)

        self.settings = QWidget()
        form_layout = QFormLayout(self.settings)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(6)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("留空则用插件的名字")
        self.title_edit.textChanged.connect(self._on_settings_changed)
        form_layout.addRow("标题", self.title_edit)

        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0.0, 86400.0)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setSpecialValueText("默认")
        self.timeout_spin.setKeyboardTracking(False)
        self.timeout_spin.valueChanged.connect(self._on_settings_changed)
        form_layout.addRow("超时", self.timeout_spin)

        self.disabled_check = QCheckBox("运行时跳过这个节点")
        self.disabled_check.toggled.connect(self._on_settings_changed)
        form_layout.addRow("", self.disabled_check)

        layout.addWidget(self.settings)
        layout.addStretch(1)

        self.set_enabled(False)

    # -- 外部接口 -------------------------------------------------------------

    def show_node(self, item: Any) -> None:
        self._current = item
        # 整个重建过程都要锁住：给控件填值会触发 changed，
        # 万一在这期间回写模型，就会把节点参数覆盖成半成品。
        self._updating = True
        try:
            if item is None:
                self.header.setText("未选择节点")
                self.subheader.setText("在画布上点一个节点")
                self.form.build({}, {}, {}, header="")
                self.set_enabled(False)
                return

            spec = item.spec
            inputs = spec.get("inputs") or {}
            upstream = {
                port: description
                for port in inputs
                if (description := self.scene.upstream_description(item.node_id, port))
            }

            self.header.setText(f"{item.node_id} · {item.title}")
            if item.missing:
                self.subheader.setText(f"插件未安装或动作不存在：{item.type_key}")
            else:
                self.subheader.setText(f"{item.type_key} · {spec.get('category', '通用')}")

            self.form.build(
                inputs,
                item.params,
                upstream,
                header="",
                outputs=spec.get("outputs") or {},
            )

            self.title_edit.setText(item.custom_title or "")
            self.timeout_spin.setValue(float(item.timeout or 0.0))
            self.disabled_check.setChecked(bool(item.disabled))
            self._show_last_output(item)
            self.set_enabled(True)
        finally:
            self._updating = False

    def set_element(self, key: str, locator: Any) -> None:
        """把拾取到的元素写回当前节点的参数，并立刻刷新界面。

        写回的一定是 ``literal`` 模式：定位器本身就是数据。哪怕这个参数原来是"连线"或
        "表达式"，也直接覆盖 —— 用户点 🎯 的意思就是"我要一个从屏幕上学来的常量"。

        整段包在 ``_updating`` 里重建表单，理由和 ``show_node`` 一样：填值会触发 changed，
        不锁住就会拿着半成品回写模型。
        """
        from kernel.graph import ParamValue  # noqa: PLC0415 - 只在拾取落盘时用

        item = self._current
        if item is None:
            return
        item.params[key] = ParamValue(kind="literal", value=locator.to_dict())
        self.show_node(item)
        self.paramChanged.emit()

    def refresh_upstream(self) -> None:
        """连线变了之后，刷新"连线"模式显示的来源。"""
        item = self._current
        if item is None:
            return
        for port in self.form.rows:
            description = self.scene.upstream_description(item.node_id, port)
            self.form.set_upstream(port, description)

    def set_enabled(self, enabled: bool) -> None:
        self.form.setEnabled(enabled)
        self.settings.setEnabled(enabled)

    # -- 内部 -----------------------------------------------------------------

    def _set_output_visible(self, show: bool) -> None:
        self.output_title.setVisible(show)
        self.output_view.setVisible(show)

    @property
    def current_node_id(self) -> str:
        """属性面板现在开着哪个节点。空字符串表示没选中。"""
        return str(getattr(self._current, "node_id", "") or "")

    def _show_last_output(self, item: Any) -> None:
        """把选中节点**上一次运行**的结果摆出来。没跑过就整块藏起来。"""
        outputs = getattr(item, "last_outputs", None) or {}
        error = str(getattr(item, "last_error", "") or "")
        if not outputs and not error:
            self._set_output_visible(False)
            self.output_view.setPlainText("")
            return

        lines: list[str] = []
        if error:
            lines.append(f"✗ 失败：{error}")
        for name, value in outputs.items():
            lines.append(f"{name} = {_pretty(value)}")
        self.output_view.setPlainText("\n".join(lines))
        self._set_output_visible(True)

    def _on_form_changed(self) -> None:
        if self._updating or self._current is None:
            return
        self._updating = True
        self._current.params = self.form.params()
        self._updating = False
        # 参数变了可能让**分支出口**增减（多路分支的「分支」那一栏）。
        # 这一步必须在这儿做：面板是直接写 item.params 的，不走 scene.set_param。
        self.scene.refresh_node_ports(self._current.node_id)
        self.paramChanged.emit()

    def _on_settings_changed(self) -> None:
        if self._updating or self._current is None:
            return
        title = self.title_edit.text().strip()
        self._current.custom_title = title or None
        self._current.timeout = self.timeout_spin.value() or None
        self._current.disabled = self.disabled_check.isChecked()
        self._current.update()
        self.paramChanged.emit()
