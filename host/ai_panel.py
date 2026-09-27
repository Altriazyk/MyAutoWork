"""AI 对话面板（骨架）。

位置在底部「运行」面板里，和运行日志、变量、运行历史并排 —— 它们回答的是同一类问题：
"现在发生了什么，接下来该做什么"。

**现在只有框架。** 对话展示、输入、"待创建的模块"卡片渲染都已经就位，但接的是一个占位
后端。换成真模型时只要 ``set_backend(...)`` 传一个 ``(prompt, history) -> AiMessage`` 的
可调用对象，界面代码一行不用改。

两个刻意的设计：

- **AI 产出的插件先进草稿卡片，不直接写盘。** 插件是会读写文件、会操作界面的代码。
  一键静默写入是不可接受的 —— 卡片上有代码预览、有校验结果，人点"保存"才落盘。
- **后端是纯函数式的调用**，不持有界面的任何引用。这样才能把它扔到线程里跑（真模型调用
  要几秒），也不会出现回调里碰控件那种随机崩溃。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from kernel.authoring import PluginDraft

from . import theme

__all__ = ["AiMessage", "AiPanel", "StubBackend"]


@dataclass
class AiMessage:
    """一轮对话里的消息。``draft`` 非空时，界面上会多出一张待保存的插件卡片。"""

    role: str  # "user" | "assistant"
    text: str = ""
    draft: PluginDraft | None = None


class AiBackend(Protocol):
    """后端只需要是一个可调用对象。不持有界面引用，方便以后扔进线程。"""

    def __call__(self, prompt: str, history: list[AiMessage]) -> AiMessage: ...  # pragma: no cover


class StubBackend:
    """占位后端：不调用任何模型，只说明这块现在是什么状态。

    故意把"我以后能做什么"写清楚，而不是回一句"功能开发中" —— 用户至少知道这一栏
    将来值不值得用。
    """

    WELCOME = (
        "这里是 myautowork 的助手。现在只有框架，还没有接入模型。\n\n"
        "接上之后打算做这几件事：\n"
        "· 用一句话描述需求，帮你把流程搭出来（选插件、连端口、填参数）\n"
        "· 写自定义模块：生成 plugins/<id>/manifest.json 和 main.py，先给你看代码再落盘\n"
        "· 某条流程失败了，帮你定位是哪个节点、哪个参数的问题\n\n"
        "界面上该有的东西都已经就位：对话、输入、以及待创建的模块卡片"
        "（卡片要等后端返回草稿才会出现）。"
    )

    REPLY = (
        "占位后端收到你的消息了，但没有模型可以回答。\n\n"
        "换成真后端要做的只有一件事：给 AiPanel.set_backend() 传一个\n"
        "    (prompt: str, history: list[AiMessage]) -> AiMessage\n"
        "可调用对象。返回值里带上 draft=PluginDraft(...)，界面上就会自动长出"
        "保存到插件目录的卡片。"
    )

    def __call__(self, prompt: str, history: list[AiMessage]) -> AiMessage:
        return AiMessage(role="assistant", text=self.REPLY)


_SEND_BUTTON_STYLE = f"""
QPushButton {{
    background: {theme.NODE_BORDER_SELECTED};
    color: #ffffff;
    font-weight: bold;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
}}
QPushButton:hover {{ background: #6fb0e8; }}
QPushButton:disabled {{ background: #3a4149; color: #6b7684; }}
"""

_SAVE_BUTTON_STYLE = f"""
QPushButton {{
    background: {theme.STATE_SUCCESS};
    color: #12281a;
    font-weight: bold;
    border: none;
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ background: #5fc074; }}
QPushButton:disabled {{ background: #3a4149; color: #6b7684; }}
"""


def _error_text(exc: BaseException) -> str:
    """把后端的异常变成对话里的一句话。

    ``AiError`` 的消息本来就是写给用户看的（"API Key 不对，去「编辑 → 设置」"），
    原样用。别的异常可能是我们自己写错了，带上类型名，方便排查 —— 但也别丢栈给用户看。
    """
    try:
        from .ai_backend import AiError  # noqa: PLC0415 - 延迟导入，避免和 ai_backend 成环
    except Exception:  # pragma: no cover
        AiError = ()  # type: ignore[assignment]
    if AiError and isinstance(exc, AiError):
        return f"⚠️ {exc}"
    return f"⚠️ 助手出错（{type(exc).__name__}）：{exc}"


class _AskThread(QThread):
    """把一次模型调用挪到后台。

    **必须挪。** 一次调用几秒到几十秒，跑在主线程里界面整个冻住 —— 连取消都点不了。
    runner 和依赖安装都是同一个理由。
    """

    replied = Signal(object)  # AiMessage

    def __init__(
        self,
        backend: Any,
        prompt: str,
        history: list["AiMessage"],
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self._prompt = prompt
        self._history = list(history)

    def run(self) -> None:  # noqa: D102 - QThread 重写
        try:
            reply = self._backend(self._prompt, self._history)
        except Exception as exc:
            reply = AiMessage(role="assistant", text=_error_text(exc))
        if not isinstance(reply, AiMessage):  # pragma: no cover - 防后端乱返回
            reply = AiMessage(role="assistant", text=str(reply))
        self.replied.emit(reply)


class _InputBox(QPlainTextEdit):
    """回车发送，Shift+回车换行。

    多行输入框默认回车是换行，直接改成发送会让想换行的人很痛苦 —— 所以留 Shift。
    """

    submitted = Signal()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        is_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if is_enter and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class AiPanel(QWidget):
    """底部「AI 对话」页。"""

    draftSaveRequested = Signal(object)  # PluginDraft
    messageSent = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._backend: Callable[[str, list[AiMessage]], AiMessage] = StubBackend()
        #: 正在跑的那次调用。非 None 时不允许再发 —— 见 send()。
        self._ask: _AskThread | None = None
        #: 「正在想…」那个占位气泡。它是临时状态，**不进历史**。
        self._pending: QWidget | None = None
        self._busy = False
        self._backend_label = ""
        self._history: list[AiMessage] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # --- 对话记录 ---
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.transcript = QWidget()
        self.transcript_layout = QVBoxLayout(self.transcript)
        self.transcript_layout.setContentsMargins(6, 6, 6, 6)
        self.transcript_layout.setSpacing(10)
        self.transcript_layout.addStretch(1)
        self.scroll.setWidget(self.transcript)
        layout.addWidget(self.scroll, 1)

        # --- 输入区 ---
        bottom = QHBoxLayout()
        bottom.setSpacing(8)

        self.input = _InputBox()
        self.input.setPlaceholderText("描述你想自动化的任务…（回车发送，Shift+回车换行）")
        self.input.setFixedHeight(62)
        self.input.submitted.connect(self.send)
        bottom.addWidget(self.input, 1)

        self.send_button = QPushButton("发送")
        self.send_button.setMinimumWidth(76)
        self.send_button.setObjectName("aiSendButton")
        self.send_button.setStyleSheet(_SEND_BUTTON_STYLE)
        self.send_button.setFixedHeight(62)
        self.send_button.clicked.connect(self.send)
        bottom.addWidget(self.send_button)

        layout.addLayout(bottom)

        self.append_message(AiMessage(role="assistant", text=StubBackend.WELCOME))

    # -- 外部接口 -------------------------------------------------------------

    def set_backend(
        self, backend: Callable[[str, list[AiMessage]], AiMessage], label: str = ""
    ) -> None:
        """换一个后端。``label`` 是给界面看的一句话（比如用的是哪个模型）。"""
        self._backend = backend
        self._backend_label = label
        self.setToolTip(label)

    @property
    def history(self) -> list[AiMessage]:
        return list(self._history)

    def append_message(self, message: AiMessage) -> None:
        """把一条消息记进历史并渲染出来。

        历史必须一起更新：后端拿到的就是这份历史，漏记的话模型会看不到自己说过什么
        （以及开场白）。
        """
        self._history.append(message)
        self._add_bubble(message)

    def _add_bubble(self, message: AiMessage) -> QWidget:
        """只渲染，**不记历史**。

        「正在想…」那种占位气泡就是靠这个 —— 它是界面上的临时状态，不是对话的一部分；
        要是记进历史，模型下一轮会看到自己"说过"一句 "正在想…"。
        """
        widget = self._bubble(message)
        self.transcript_layout.insertWidget(self.transcript_layout.count() - 1, widget)
        self._scroll_to_bottom()
        return widget

    # -- 交互 ----------------------------------------------------------------

    def send(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        if self._ask is not None:
            # **不排队。** 排队的话用户发完看不到任何反应，会以为卡住了；
            # 直说"上一条还在等"比默默攒着强。
            self.append_message(
                AiMessage(role="assistant", text="上一条还在等回复，等它回来再发下一条。")
            )
            return

        self.input.clear()
        self.messageSent.emit(text)
        self.append_message(AiMessage(role="user", text=text))

        self._pending = self._add_bubble(AiMessage(role="assistant", text="正在想…"))
        self._set_busy(True)

        self._ask = _AskThread(self._backend, text, self.history, self)
        self._ask.replied.connect(self._on_replied)
        self._ask.start()

    def _on_replied(self, reply: AiMessage) -> None:
        self._ask = None
        if self._pending is not None:
            # 占位气泡要先撤掉再追加真回复，否则它会一直挂在上面。
            self._pending.setParent(None)
            self._pending.deleteLater()
            self._pending = None
        self._set_busy(False)
        self.append_message(reply)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.send_button.setEnabled(not busy)
        self.send_button.setText("思考中…" if busy else "发送")

    @property
    def busy(self) -> bool:
        return self._busy

    def reset(self, welcome: str) -> None:
        """清空对话，重新起一个开场白。**换了后端之后用。**

        留着旧开场白是会被用户当真的 —— 占位后端那句"还没有接入模型"配上真后端就是假话。
        """
        self._history.clear()
        while self.transcript_layout.count():
            item = self.transcript_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.append_message(AiMessage(role="assistant", text=welcome))

    def _scroll_to_bottom(self) -> None:
        # 等布局跑完再滚，否则滚的是旧高度。
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()
        ))

    # -- 渲染 ----------------------------------------------------------------

    def _bubble(self, message: AiMessage) -> QWidget:
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)

        frame = QFrame()
        frame.setObjectName("aiBubbleUser" if message.role == "user" else "aiBubble")
        frame.setMaximumWidth(720)

        box = QVBoxLayout(frame)
        box.setContentsMargins(12, 9, 12, 9)
        box.setSpacing(8)

        if message.text:
            label = QLabel(message.text)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            box.addWidget(label)

        if message.draft is not None:
            box.addWidget(self._draft_card(message.draft))

        if message.role == "user":
            row.addStretch(1)
            row.addWidget(frame)
        else:
            row.addWidget(frame)
            row.addStretch(1)
        return wrapper

    def _draft_card(self, draft: PluginDraft) -> QWidget:
        card = QFrame()
        card.setObjectName("aiDraftCard")

        box = QVBoxLayout(card)
        box.setContentsMargins(10, 9, 10, 9)
        box.setSpacing(6)

        title = QLabel(f"待创建的模块：{draft.summary()}")
        title.setStyleSheet("font-weight: bold;")
        box.addWidget(title)

        if draft.description:
            description = QLabel(draft.description)
            description.setWordWrap(True)
            description.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
            box.addWidget(description)

        problems = draft.validate()
        if problems:
            problem_label = QLabel("校验未通过：\n· " + "\n· ".join(problems))
            problem_label.setWordWrap(True)
            problem_label.setStyleSheet(f"color: {theme.STATE_FAILED}; font-size: 11px;")
            box.addWidget(problem_label)

        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setFixedHeight(130)
        preview.setPlainText(draft.code)
        font = preview.font()
        font.setFamilies(["Cascadia Mono", "Consolas", "Courier New", "monospace"])
        preview.setFont(font)
        box.addWidget(preview)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        save_button = QPushButton("保存到插件目录")
        save_button.setStyleSheet(_SAVE_BUTTON_STYLE)
        save_button.setEnabled(not problems)
        if problems:
            save_button.setToolTip("先修好上面的问题")
        save_button.clicked.connect(lambda: self.draftSaveRequested.emit(draft))
        actions.addWidget(save_button)

        actions.addStretch(1)
        box.addLayout(actions)
        return card
