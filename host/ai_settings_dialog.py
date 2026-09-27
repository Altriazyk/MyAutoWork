"""AI 设置的编辑窗口。

**为什么是对话框而不是又一个页面。** 这里是"填一次就不动"的配置，不是日常操作的界面。
放进左侧导航会让人以为它是第四个功能页；做成对话框，改完就走。

**「测试连接」是这一页最重要的按钮。** 地址、模型名、密钥这三样里任何一样写错，症状都是
"AI 不回答" —— 光看对话根本分不出是哪一样。所以保存之前就能点一下，让错误当场暴露，
而不是等到用的时候才发现。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .ai_backend import AiError, ChatBackend
from .ai_panel import AiMessage
from .ai_settings import ENV_API_KEY, AiSettings, resolve_api_key, save_settings, settings_path

__all__ = ["AiSettingsPage", "SettingsDialog"]


class _ProbeThread(QThread):
    """后台试一次调用，免得点「测试连接」时界面冻住。"""

    done = Signal(bool, str)

    def __init__(self, settings: AiSettings, parent: Any = None) -> None:
        super().__init__(parent)
        self._settings = settings

    def run(self) -> None:  # noqa: D102 - QThread 重写
        try:
            reply = ChatBackend(self._settings)("你好，只回两个字：收到", [])
        except AiError as exc:
            self.done.emit(False, str(exc))
        except Exception as exc:  # pragma: no cover - 兜底
            self.done.emit(False, f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(True, reply.text.strip()[:200])


class AiSettingsPage(QWidget):
    """AI 助手那一页：接口、密钥、模型，以及告诉模型 SDK 长什么样的系统提示词。

    以前它是一个独立的对话框，现在是「设置」里的一页 —— 内容没变，只是不再自己
    拥有窗口和按钮。
    """

    def __init__(self, settings: AiSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._probe: _ProbeThread | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 0, 0)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(9)

        self.base_url = QLineEdit(settings.base_url)
        self.base_url.setPlaceholderText("https://api.deepseek.com/v1")
        form.addRow("接口地址", self.base_url)

        key_row = QHBoxLayout()
        self.api_key = QLineEdit(settings.api_key)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("sk-…")
        key_row.addWidget(self.api_key, 1)
        self.show_key = QCheckBox("显示")
        self.show_key.toggled.connect(
            lambda shown: self.api_key.setEchoMode(
                QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
            )
        )
        key_row.addWidget(self.show_key)
        holder = QWidget()
        holder.setLayout(key_row)
        form.addRow("API Key", holder)

        self.model = QLineEdit(settings.model)
        self.model.setPlaceholderText("deepseek-chat")
        form.addRow("模型", self.model)

        numbers = QHBoxLayout()
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.1)
        self.temperature.setValue(float(settings.temperature))
        numbers.addWidget(QLabel("温度"))
        numbers.addWidget(self.temperature)
        numbers.addSpacing(18)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(5.0, 600.0)
        self.timeout.setSingleStep(5.0)
        self.timeout.setSuffix(" 秒")
        self.timeout.setValue(float(settings.timeout))
        numbers.addWidget(QLabel("超时"))
        numbers.addWidget(self.timeout)
        numbers.addStretch(1)
        numbers_holder = QWidget()
        numbers_holder.setLayout(numbers)
        form.addRow("参数", numbers_holder)

        layout.addLayout(form)

        # --- 密钥存在哪，说清楚 ---
        stored = settings_path()
        note = QLabel(
            f"密钥会**明文**存在 {stored}。\n"
            f"不想落盘的话把它留空，改成设一个环境变量 {ENV_API_KEY} —— 那样更安全，"
            "程序会优先用它。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        layout.addWidget(note)

        # --- 系统提示词 ---
        layout.addWidget(QLabel("系统提示词（告诉模型这套 SDK 长什么样）"))
        self.system_prompt = QPlainTextEdit(settings.system_prompt)
        self.system_prompt.setMinimumHeight(160)
        layout.addWidget(self.system_prompt, 1)

        # --- 测试连接 ---
        test_row = QHBoxLayout()
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self._probe_connection)
        test_row.addWidget(self.test_button)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        test_row.addWidget(self.test_result, 1)
        layout.addLayout(test_row)

    # -- 取值 ----------------------------------------------------------------

    def settings(self) -> AiSettings:
        """把界面上的值收成一个 AiSettings。"""
        return AiSettings(
            base_url=self.base_url.text().strip(),
            api_key=self.api_key.text().strip(),
            model=self.model.text().strip(),
            temperature=float(self.temperature.value()),
            timeout=float(self.timeout.value()),
            system_prompt=self.system_prompt.toPlainText(),
        )

    def save(self) -> Any:
        return save_settings(self.settings())

    # -- 测试 ----------------------------------------------------------------

    def _probe_connection(self) -> None:
        if self._probe is not None:
            return
        candidate = self.settings()
        if not resolve_api_key(candidate):
            self.test_result.setText("先填 API Key 再测")
            self.test_result.setStyleSheet(f"color: {theme.STATE_WARNING};")
            return

        self.test_button.setEnabled(False)
        self.test_result.setText("正在试……")
        self.test_result.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")

        self._probe = _ProbeThread(candidate, self)
        self._probe.done.connect(self._on_probe_done)
        self._probe.start()

    def _on_probe_done(self, ok: bool, message: str) -> None:
        self._probe = None
        self.test_button.setEnabled(True)
        color = theme.STATE_SUCCESS if ok else theme.STATE_FAILED
        self.test_result.setStyleSheet(f"color: {color};")
        self.test_result.setText(f"通了，模型回了：{message}" if ok else message)


class SettingsDialog(QDialog):
    """统一的设置窗口。左边选类别，右边是内容。

    **为什么把 AI 设置收进这里。** 它本来是独立对话框，挂在「编辑 → AI 设置…」。
    但设置是个会一直长的东西（外观、运行、快捷键……），每加一项就多一个菜单项、多一个
    窗口，菜单越来越长，用户还得记住每项在哪。收成一个入口之后，**加设置只是加一页**。
    """

    def __init__(self, ai_settings: AiSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumSize(780, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(12)

        body = QHBoxLayout()
        body.setSpacing(14)

        self.categories = QListWidget()
        self.categories.setFixedWidth(150)
        self.pages = QStackedWidget()
        body.addWidget(self.categories)
        body.addWidget(self.pages, 1)
        layout.addLayout(body, 1)

        # 加设置项就是加这一行。
        self.ai_page = AiSettingsPage(ai_settings)
        self._add_page("AI 助手", self.ai_page)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.categories.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.categories.setCurrentRow(0)

    def _add_page(self, title: str, page: QWidget) -> None:
        self.categories.addItem(title)
        self.pages.addWidget(page)

    def ai_settings(self) -> AiSettings:
        return self.ai_page.settings()
