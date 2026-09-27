"""插件页：把注册表里的插件目录摊开给人看。

命令行用 ``--list-plugins`` 也能看到同样的信息，但那个输出是给开发者看的。这一页是给
"我想知道现在装着什么、每个能干什么"的用户看的。

数据全部来自 ``registry.catalog()`` —— 也就是插件自己声明的接口。所以这一页同样是
"装一个插件就多一块内容，界面代码一行不改"。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .card_wall import TILE_HEIGHT, TILE_WIDTH, CardWall, ViewModeToggle

__all__ = ["PluginListView", "PluginCard", "PluginTile"]

_CARD_STYLE = f"""
QFrame#pluginCard {{
    background: {theme.NODE_BODY};
    border: 1px solid {theme.NODE_BORDER};
    border-radius: 10px;
}}
"""

#: 「详情」「位置」这类普通按钮：度量必须和删除按钮一致，只有颜色不同。
_PLAIN_BUTTON_STYLE = f"""
QPushButton {{
    background: transparent; color: {theme.NODE_TEXT};
    border: 1px solid {theme.NODE_BORDER}; border-radius: 6px; padding: 7px 10px;
}}
QPushButton:hover {{ border-color: {theme.NODE_BORDER_SELECTED}; color: #ffffff; }}
QPushButton:disabled {{ color: #6b7684; border-color: #3a4149; background: transparent; }}
"""

#: 删除按钮。**度量跟着「我的模块」「我的流程」那一套走**（padding 7/10、圆角 6）
#: —— 同一个位置的同类按钮，换个页面不该换一副长相。
_DELETE_BUTTON_STYLE = f"""
QPushButton {{
    background: transparent; color: {theme.STATE_FAILED};
    border: 1px solid {theme.STATE_FAILED}; border-radius: 6px; padding: 7px 10px;
}}
QPushButton:hover {{ background: {theme.STATE_FAILED}; color: #1b1f25; }}
QPushButton:disabled {{ border-color: #3a4149; color: #6b7684; background: transparent; }}
"""


def _action_row(buttons: list[QPushButton]) -> QHBoxLayout:
    """整行平铺的按钮行 —— 「我的插件」「我的模块」「我的流程」共用这一套度量。

    度量全部取自 ``theme``，不在各处再写一遍：之前三个页面各写一份，结果是高度、
    间距、padding 三处都不一样。
    """
    row = QHBoxLayout()
    row.setSpacing(theme.ROW_BUTTON_SPACING)
    for button in buttons:
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setFixedHeight(theme.ROW_BUTTON_HEIGHT)
        row.addWidget(button, 1)
    return row


def _port_summary(spec: dict[str, Any]) -> str:
    inputs = len(spec.get("inputs") or {})
    outputs = len(spec.get("outputs") or {})
    return f"{inputs} 入 / {outputs} 出"


def _port_tooltip(spec: dict[str, Any]) -> str:
    lines: list[str] = []
    inputs = spec.get("inputs") or {}
    outputs = spec.get("outputs") or {}
    if inputs:
        lines.append("输入：")
        for name, schema in inputs.items():
            label = schema.get("label") or name
            required = "（必填）" if schema.get("required") else ""
            lines.append(f"  {label} · {name} · {schema.get('kind', 'any')}{required}")
    if outputs:
        lines.append("输出：")
        for name, schema in outputs.items():
            label = schema.get("label") or name
            lines.append(f"  {label} · {name} · {schema.get('kind', 'any')}")
    return "\n".join(lines) or "没有输入输出"


class PluginCard(QFrame):
    """一个插件一块。"""

    deleteRequested = Signal(str)
    revealRequested = Signal(str)
    detailsRequested = Signal(str)

    def __init__(self, plugin: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pluginCard")
        self.setStyleSheet(_CARD_STYLE)
        self.plugin_id = str(plugin.get("id") or "")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(5)

        # --- 标题行 ---
        head = QHBoxLayout()
        head.setSpacing(8)

        dot = QLabel("\u25cf")
        dot.setStyleSheet(f"color: {theme.category_color(str(plugin.get('category') or '通用'))};")
        head.addWidget(dot)

        name = QLabel(str(plugin.get("name") or plugin.get("id")))
        name_font = QFont()
        name_font.setPointSizeF(11.0)
        name_font.setBold(True)
        name.setFont(name_font)
        head.addWidget(name)

        version = QLabel(f"v{plugin.get('version', '?')}")
        version.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        head.addWidget(version)

        head.addStretch(1)

        plugin_id = QLabel(str(plugin.get("id")))
        plugin_id.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        head.addWidget(plugin_id)
        layout.addLayout(head)

        description = plugin.get("description")
        if description:
            text = QLabel(str(description))
            text.setWordWrap(True)
            text.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
            layout.addWidget(text)

        load_error = plugin.get("load_error")
        if load_error:
            error = QLabel(f"加载失败：{load_error}")
            error.setWordWrap(True)
            error.setStyleSheet(f"color: {theme.STATE_FAILED}; font-size: 11px;")
            layout.addWidget(error)

        # --- 能力清单 ---
        entries = list(plugin.get("triggers") or []) + list(plugin.get("actions") or [])
        if not entries:
            empty = QLabel("静态声明里没有动作（进程没能加载）")
            empty.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
            layout.addWidget(empty)
        for spec in entries:
            layout.addWidget(self._ability_row(spec))

        # --- 删除 ---
        # 靠右单独一行：破坏性操作不该混在能力清单里，也不该紧挨着别的东西。
        # 按钮行整行平铺，和「我的模块」「我的流程」同一个做法（共用 _action_row）。
        self.details_button = QPushButton("详情")
        self.details_button.setStyleSheet(_PLAIN_BUTTON_STYLE)
        self.details_button.setToolTip("这个插件的全部信息：能力、参数、路径、依赖")
        self.details_button.clicked.connect(lambda: self.detailsRequested.emit(self.plugin_id))
        self.reveal_button = QPushButton("位置")
        self.reveal_button.setStyleSheet(_PLAIN_BUTTON_STYLE)
        self.reveal_button.setToolTip("在资源管理器里打开它的文件夹")
        self.reveal_button.clicked.connect(lambda: self.revealRequested.emit(self.plugin_id))
        self.delete_button = QPushButton("删除")
        self.delete_button.setStyleSheet(_DELETE_BUTTON_STYLE)
        self.delete_button.setToolTip(
            "删到回收站，误删还能捞回来。\n"
            "注意：画布上用到它的流程会失效 —— 点的时候会先列出是哪些"
        )
        self.delete_button.clicked.connect(lambda: self.deleteRequested.emit(self.plugin_id))
        layout.addLayout(
            _action_row([self.details_button, self.reveal_button, self.delete_button])
        )

    def _ability_row(self, spec: dict[str, Any]) -> QWidget:
        row = QWidget()
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        is_trigger = spec.get("kind") == "trigger"
        badge = QLabel("触发" if is_trigger else "动作")
        badge.setStyleSheet(
            "background: %s; color: #1b1f25; border-radius: 4px;"
            "font-size: 10px; font-weight: bold; padding: 1px 6px;"
            % (theme.STATE_RUNNING if is_trigger else theme.NODE_BORDER_SELECTED)
        )
        box.addWidget(badge)

        name = QLabel(str(spec.get("name") or spec.get("id")))
        box.addWidget(name)

        ident = QLabel(str(spec.get("id")))
        ident.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        box.addWidget(ident)

        box.addStretch(1)

        ports = QLabel(_port_summary(spec))
        ports.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        box.addWidget(ports)

        row.setToolTip(f"{spec.get('id')}\n{spec.get('description', '')}\n\n{_port_tooltip(spec)}")
        return row


class PluginTile(QFrame):
    """网格视图里的一块方砖。信息比列表行少，只留"这是什么、能干什么"。"""

    deleteRequested = Signal(str)
    revealRequested = Signal(str)
    detailsRequested = Signal(str)

    def __init__(self, plugin: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plugin_id = str(plugin.get("id") or "")
        self.setObjectName("pluginCard")
        self.setStyleSheet(_CARD_STYLE)
        self.setFixedSize(TILE_WIDTH, TILE_HEIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        dot = QLabel("\u25cf")
        dot.setStyleSheet(f"color: {theme.category_color(str(plugin.get('category') or '通用'))};")
        head.addWidget(dot)

        name = QLabel(str(plugin.get("name") or plugin.get("id")))
        name_font = QFont()
        name_font.setPointSizeF(11.0)
        name_font.setBold(True)
        name.setFont(name_font)
        name.setToolTip(str(plugin.get("id")))
        head.addWidget(name, 1)

        version = QLabel(f"v{plugin.get('version', '?')}")
        version.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        head.addWidget(version)
        layout.addLayout(head)

        description = QLabel(str(plugin.get("description") or "（没有填写描述）"))
        description.setWordWrap(True)
        description.setFixedHeight(36)
        description.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        description.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        layout.addWidget(description)

        load_error = plugin.get("load_error")
        if load_error:
            error = QLabel("加载失败")
            error.setStyleSheet(f"color: {theme.STATE_FAILED}; font-size: 11px;")
            error.setToolTip(str(load_error))
            layout.addWidget(error)

        layout.addStretch(1)

        actions = list(plugin.get("actions") or [])
        triggers = list(plugin.get("triggers") or [])
        counts = QLabel(f"{len(actions)} 个动作 · {len(triggers)} 个触发器")
        counts.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        layout.addWidget(counts)
        self.setToolTip(f"{plugin.get('id')}\n{plugin.get('description', '')}")

        self.details_button = QPushButton("详情")
        self.details_button.setStyleSheet(_PLAIN_BUTTON_STYLE)
        self.details_button.setToolTip("这个插件的全部信息")
        self.details_button.clicked.connect(lambda: self.detailsRequested.emit(self.plugin_id))
        self.reveal_button = QPushButton("位置")
        self.reveal_button.setStyleSheet(_PLAIN_BUTTON_STYLE)
        self.reveal_button.setToolTip("在资源管理器里打开它的文件夹")
        self.reveal_button.clicked.connect(lambda: self.revealRequested.emit(self.plugin_id))
        self.delete_button = QPushButton("删除")
        self.delete_button.setStyleSheet(_DELETE_BUTTON_STYLE)
        self.delete_button.setToolTip("删到回收站，误删还能捞回来")
        self.delete_button.clicked.connect(lambda: self.deleteRequested.emit(self.plugin_id))
        layout.addLayout(
            _action_row([self.details_button, self.reveal_button, self.delete_button])
        )


class PluginDetailsDialog(QDialog):
    """一个插件的全部信息。

    卡片上放不下这些（能力清单、每个动作要什么参数、依赖装了没、原始 manifest），
    但"它为什么加载失败""这个动作到底要填什么"恰好就是需要看这些的时候。

    用一块只读文本而不是一堆控件：**能选中、能复制**。用户要把这些贴进提问或者日志里时，
    能复制比排版整齐重要得多。
    """

    def __init__(self, plugin: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"插件详情 · {plugin.get('name') or plugin.get('id')}")
        self.setMinimumSize(700, 580)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setPlainText(self._report(plugin))
        body.setStyleSheet("font-family: Consolas, monospace;")
        layout.addWidget(body, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _report(plugin: dict[str, Any]) -> str:
        lines: list[str] = []
        lines.append(f"{plugin.get('name')}（{plugin.get('id')}）")
        lines.append(
            f"版本 {plugin.get('version', '?')} · 契约 {plugin.get('api_version', '?')}"
            f" · 分类 {plugin.get('category', '?')}"
        )
        if plugin.get("author"):
            lines.append(f"作者 {plugin['author']}")
        if plugin.get("description"):
            lines.append("")
            lines.append(str(plugin["description"]))

        lines.append("")
        lines.append("── 状态 ──")
        if plugin.get("load_error"):
            lines.append(f"⚠ 加载失败：{plugin['load_error']}")
        elif plugin.get("loaded"):
            lines.append("已加载，进程正常")
        else:
            lines.append("还没加载过")

        lines.append("")
        lines.append("── 位置 ──")
        lines.append(f"目录：{plugin.get('path', '?')}")
        runtime = plugin.get("runtime") or {}
        lines.append(f"入口：{runtime.get('entry', 'main.py')}")
        if runtime.get("venv"):
            lines.append(f"自带环境：{runtime['venv']}")
        if runtime.get("python"):
            lines.append(f"指定解释器：{runtime['python']}")

        lines.append("")
        declared = list(plugin.get("dependencies") or [])
        missing = list(plugin.get("missing_dependencies") or [])
        lines.append("── 依赖 ──")
        if not declared:
            lines.append("（没有声明）")
        else:
            for item in declared:
                lines.append(f"{'⚠ 缺 ' if item in missing else '  有 '}{item}")

        for kind, title in (("triggers", "触发器"), ("actions", "动作")):
            specs = list(plugin.get(kind) or [])
            lines.append("")
            lines.append(f"── {title}（{len(specs)} 个）──")
            if not specs:
                lines.append("（无）")
            for spec in specs:
                lines.append(f"· {spec.get('name')}  [{spec.get('id')}]")
                if spec.get("description"):
                    lines.append(f"    {spec['description']}")
                for name, schema in (spec.get("inputs") or {}).items():
                    required = "必填" if (schema or {}).get("required") else "可选"
                    default = (schema or {}).get("default")
                    hint = f"，默认 {default!r}" if default is not None else ""
                    lines.append(
                        f"    入 {name}（{(schema or {}).get('kind', '?')}，{required}{hint}）"
                    )
                for name, schema in (spec.get("outputs") or {}).items():
                    lines.append(f"    出 {name}（{(schema or {}).get('kind', '?')}）")
                    _ = schema

        lines.append("")
        lines.append("── 原始 manifest.json ──")
        try:
            import json  # noqa: PLC0415

            lines.append(json.dumps(plugin.get("raw") or {}, ensure_ascii=False, indent=2))
        except Exception:  # pragma: no cover - 理论上不会走到
            lines.append(str(plugin.get("raw")))
        return "\n".join(lines)


class PluginListView(QWidget):
    """**内置**插件一览。两种排法，切换按钮在这一页的右上角。

    **只列随程序发出去的那些。** 用户自己的模块归「我的模块」页 —— 两页各管一类，
    才不会出现"同一个模块在两个地方都能看到、但只有一个地方能删"这种别扭。

    两页各管一类 —— 「我的模块」管自己写的（能改代码、能装依赖），这一页是随程序发出去的
    那些。**两边都能删**，因为内置插件的误删也走回收站，捞得回来。
    """

    deleteRequested = Signal(str)
    revealRequested = Signal(str)
    detailsRequested = Signal(str)

    #: 要装一个插件包（压缩包）。路径由界面那边选。
    installRequested = Signal()

    def __init__(self, registry: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.registry = registry
        self._plugins: list[dict[str, Any]] = []
        self.cards: dict[str, PluginCard] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        head = QHBoxLayout()
        heading = QLabel("我的插件")
        heading_font = QFont()
        heading_font.setPointSizeF(14.0)
        heading_font.setBold(True)
        heading.setFont(heading_font)
        head.addWidget(heading)

        self.summary = QLabel()
        self.summary.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        head.addWidget(self.summary)
        head.addStretch(1)

        self.toggle = ViewModeToggle()
        self.toggle.changed.connect(self.set_view_mode)
        head.addWidget(self.toggle)

        self.install_button = QPushButton("安装插件包…")
        self.install_button.setToolTip(
            "选一个插件压缩包（.zip），解压到插件目录。\n"
            "包里应该有 manifest.json 和 main.py —— 在压缩包根下，或者外面只套一层文件夹"
        )
        self.install_button.clicked.connect(self.installRequested)
        head.addWidget(self.install_button)
        layout.addLayout(head)

        self.wall = CardWall(self._make_tile, self._make_card)
        self.wall.hint_text = self._empty_hint
        layout.addWidget(self.wall, 1)

        self.reload()

    # -- 工厂 ----------------------------------------------------------------

    def _make_tile(self, plugin: dict[str, Any]) -> QWidget:
        tile = PluginTile(plugin)
        tile.deleteRequested.connect(self.deleteRequested)
        tile.revealRequested.connect(self.revealRequested)
        tile.detailsRequested.connect(self.detailsRequested)
        return tile

    def _make_card(self, plugin: dict[str, Any]) -> QWidget:
        card = PluginCard(plugin)
        card.deleteRequested.connect(self.deleteRequested)
        card.revealRequested.connect(self.revealRequested)
        card.detailsRequested.connect(self.detailsRequested)
        self.cards[str(plugin.get("id"))] = card
        return card

    def _empty_hint(self) -> str:
        # 只给文字。居中交给 CardWall —— 它把提示放在栈外面，那里天然占满整行。
        return (
            "一个内置插件都没有 —— plugins/ 目录不完整。\n"
            "（自己写的模块在左侧「我的模块」页。）"
        )

    # -- 数据 ----------------------------------------------------------------

    def reload(self) -> None:
        self.cards.clear()
        # 只看内置的。用户自己的模块在「我的模块」页，不在这儿重复出现。
        self._plugins = [
            entry
            for entry in self.registry.catalog()
            if self.registry.is_builtin(str(entry.get("id") or ""))
        ]
        self.wall.set_items(self._plugins)

        abilities = sum(
            len(plugin.get("actions") or []) + len(plugin.get("triggers") or [])
            for plugin in self._plugins
        )
        broken = sum(1 for plugin in self._plugins if plugin.get("load_error"))
        text = f"共 {len(self._plugins)} 个插件 · {abilities} 个能力"
        if broken:
            text += f" · {broken} 个加载失败"
        self.summary.setText(text)

    def set_view_mode(self, mode: str) -> None:
        self.wall.set_view_mode(mode)
        self.toggle.set_mode(mode)

    @property
    def view_mode(self) -> str:
        return self.wall.view_mode

    def plugin_count(self) -> int:
        return len(self._plugins)

    def card_for(self, plugin_id: str) -> PluginCard | None:
        return self.cards.get(plugin_id)
