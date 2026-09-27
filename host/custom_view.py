"""自定义模块页。

用户自己的模块 —— 用底部 AI 助手生成的，或者自己写好一个插件文件夹丢进 ``plugins/`` 的。
这一页只干两件事：**让你看清有哪些**，以及**让你能删掉它们**（连带删掉文件夹）。

**为什么不和「插件」页合并。** 那页回答"我现在有什么能力"，这页回答"哪些是我自己加的、
不要了可以清掉"。混在一起会让内置插件也显得可以删 —— 而删掉 ``win.uia`` 之后画布上所有
界面自动化节点会一起变成"插件未安装"，这种事故不该由一个看起来无害的删除按钮引起。

**内置 / 自定义怎么区分。** 看 manifest 里的 ``"builtin": true``。默认当成"可删"是危险的
方向，所以反过来：只有明确标了内置的才保护。这样用户手写的插件不需要额外登记就天然能被
管理到。

**删除前为什么要数一遍有多少流程在用。** 删文件夹是不可逆的，而"有 3 条流程在用它"这句
话是用户唯一能拿到的、判断该不该删的依据。这个统计不走 ``WorkflowLibrary`` —— 那个库为了
首页启动快，只读文件头、不解析节点。删除是低频且危险的操作，慢一点没关系。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .card_wall import TILE_HEIGHT, TILE_WIDTH, CardWall, ViewModeToggle

__all__ = ["CustomModuleView", "CustomModuleRow", "ModuleTile", "workflows_using"]

_TILE_BUTTON_STYLE = f"""
QPushButton {{
    background: transparent;
    color: {theme.NODE_TEXT};
    border: 1px solid {theme.NODE_BORDER};
    border-radius: 6px;
    padding: 7px 10px;
}}
QPushButton:hover {{ border-color: {theme.NODE_BORDER_SELECTED}; color: #ffffff; }}
QPushButton:disabled {{ color: #6b7684; border-color: #3a4149; }}
"""


def workflows_using(workflows_dir: Path, plugin_id: str) -> list[str]:
    """哪些流程用到了这个模块。返回流程名（排序）。解析失败的流程直接跳过。"""
    used: list[str] = []
    if not workflows_dir.is_dir():
        return used
    for path in sorted(workflows_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        nodes = data.get("nodes") or []
        if not isinstance(nodes, list):
            continue
        if any(
            isinstance(node, dict) and node.get("plugin") == plugin_id for node in nodes
        ):
            used.append(str(data.get("name") or path.stem))
    return sorted(used)


_TILE_BUTTON_HEIGHT = theme.ROW_BUTTON_HEIGHT

#: 方砖里的「删除」。**度量必须和 ``_TILE_BUTTON_STYLE`` 完全一致** —— 同一行里的按钮
#: 只有颜色该不同。之前它套的是列表行那一套（padding 6/14、字号也更大），结果在方砖里
#: 比旁边三个明显胖一圈、高一截，四个按钮排在一起参差不齐。
_TILE_DELETE_STYLE = f"""
QPushButton {{
    background: transparent;
    color: {theme.STATE_FAILED};
    border: 1px solid {theme.STATE_FAILED};
    border-radius: 6px;
    padding: 7px 10px;
}}
QPushButton:hover {{ background: {theme.STATE_FAILED}; color: #1b1f25; }}
QPushButton:disabled {{ color: #6b7684; border-color: #3a4149; background: transparent; }}
"""

_INSTALL_STYLE = f"""
QPushButton {{
    background: transparent; color: {theme.STATE_WARNING};
    border: 1px solid {theme.STATE_WARNING}; border-radius: 6px; padding: 5px 12px;
}}
QPushButton:hover {{ background: {theme.STATE_WARNING}; color: #1b1f25; }}
QPushButton:disabled {{ border-color: #3a4149; color: #6b7684; background: transparent; }}
"""

_DELETE_STYLE = f"""
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


class CustomModuleRow(QFrame):
    """一个自定义模块。占满一行。"""

    deleteRequested = Signal(str)
    editRequested = Signal(str)
    renameRequested = Signal(str)
    revealRequested = Signal(str)
    #: 这条模块缺依赖，要装（模块 id, pip 要求列表）
    installRequested = Signal(str, list)

    def __init__(self, info: dict[str, Any], used_by: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.info = info
        self.plugin_id = str(info["id"])

        self.setObjectName("customRow")
        self.setStyleSheet(
            f"QFrame#customRow {{ background: {theme.NODE_BODY};"
            f" border: 1px solid {theme.NODE_BORDER}; border-radius: 10px; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 14, 12)
        outer.setSpacing(5)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        name = QLabel(str(info.get("name") or self.plugin_id))
        font = QFont()
        font.setPointSizeF(11.5)
        font.setBold(True)
        name.setFont(font)
        title_row.addWidget(name)

        ident = QLabel(f"{self.plugin_id}  v{info.get('version') or '?'}")
        ident.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        title_row.addWidget(ident)
        title_row.addStretch(1)
        outer.addLayout(title_row)

        description = QLabel(str(info.get("description") or "（没有填写描述）"))
        description.setWordWrap(True)
        description.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        outer.addWidget(description)

        summary = self._summary(info)
        if info.get("load_error"):
            summary += f"  ·  加载失败：{info['load_error']}"
        outer.addWidget(self._meta(summary, bool(info.get("load_error"))))

        # 命中流程这一行是删除前唯一的判断依据，所以单独一行、并且用醒目色。
        if used_by:
            preview = "、".join(used_by[:4]) + ("…" if len(used_by) > 4 else "")
            outer.addWidget(
                self._meta(f"有 {len(used_by)} 条流程在用它：{preview}", False, warn=True)
            )

        # 缺依赖时给一个「安装依赖」按钮，**单独占一行** —— 页面上放一个总的，用户还得
        # 回头去找是哪条模块缺；而"装不装"这个决定本来就是针对具体某条模块做的。
        self.install_button: QPushButton | None = None
        missing = list(info.get("missing_dependencies") or [])
        if missing:
            install = QPushButton("安装依赖")
            install.setStyleSheet(_INSTALL_STYLE)
            install.setToolTip("缺：" + "、".join(missing) + "\n点一下在当前环境里装上")
            install.clicked.connect(
                lambda: self.installRequested.emit(self.plugin_id, missing)
            )
            # 单独占一行：它是个"缺东西了，点一下修"的提示，和下面四个操作按钮不是一类。
            outer.addWidget(install)
            self.install_button = install

        # 四个操作按钮等宽平铺整行 —— 和网格方砖、流程卡片用同一套度量（theme 里那份）。
        bottom = QHBoxLayout()
        bottom.setSpacing(theme.ROW_BUTTON_SPACING)
        row_buttons: list[QPushButton] = []

        # 改 / 删 都放在这一行上。以前它们在左侧栏的右键菜单里 —— 但左侧栏现在只有一个
        # 「我的模块」导航项，模块是列在这一页里的，所以操作也该在这一页上。
        code = QPushButton("编辑代码")
        code.setToolTip("在右侧打开这个模块的 main.py")
        code.clicked.connect(lambda: self.editRequested.emit(self.plugin_id))
        row_buttons.append(code)

        rename = QPushButton("重命名")
        rename.setToolTip("只改显示名；标识和目录不动，用到它的流程不受影响")
        rename.clicked.connect(lambda: self.renameRequested.emit(self.plugin_id))
        row_buttons.append(rename)

        reveal = QPushButton("位置")
        reveal.setToolTip("在资源管理器里打开这个模块的文件夹")
        reveal.clicked.connect(lambda: self.revealRequested.emit(str(info.get("path") or "")))
        row_buttons.append(reveal)

        self.delete_button = QPushButton("删除")
        self.delete_button.setStyleSheet(_DELETE_STYLE)
        self.delete_button.setToolTip("连同这个模块的文件夹一起删除")
        self.delete_button.clicked.connect(lambda: self.deleteRequested.emit(self.plugin_id))
        row_buttons.append(self.delete_button)

        for button in row_buttons:
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setFixedHeight(theme.ROW_BUTTON_HEIGHT)
            bottom.addWidget(button, 1)
        outer.addLayout(bottom)

    @staticmethod
    def _summary(info: dict[str, Any]) -> str:
        actions = list(info.get("actions") or [])
        triggers = list(info.get("triggers") or [])
        parts = [f"{len(actions)} 个动作", f"{len(triggers)} 个触发器"]
        if actions:
            names = "、".join(str(a.get("name") or a.get("id")) for a in actions[:5])
            parts.append(names + ("…" if len(actions) > 5 else ""))
        return "  ·  ".join(parts)

    @staticmethod
    def _meta(text: str, is_error: bool, warn: bool = False) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        if is_error:
            color = theme.STATE_FAILED
        elif warn:
            color = theme.STATE_WARNING
        else:
            color = theme.NODE_SUBTITLE
        label.setStyleSheet(f"color: {color}; font-size: 11px;")
        return label


class ModuleTile(QFrame):
    """网格视图里的一块方砖。按钮用短标签，四个才排得下。"""

    deleteRequested = Signal(str)
    editRequested = Signal(str)
    renameRequested = Signal(str)
    revealRequested = Signal(str)
    installRequested = Signal(str, list)

    def __init__(self, info: dict[str, Any], used_by: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plugin_id = str(info["id"])
        #: 「位置」要的是目录路径，不是标识 —— 单独存一份，别指望用时能拼出来
        #: （``plugins_dir / plugin_id`` 看着行得通，但 manifest 里可以写别的路径）。
        self.path = str(info.get("path") or "")

        self.setObjectName("customTile")
        self.setStyleSheet(
            f"QFrame#customTile {{ background: {theme.NODE_BODY};"
            f" border: 1px solid {theme.NODE_BORDER}; border-radius: 10px; }}"
        )
        self.setFixedSize(TILE_WIDTH, TILE_HEIGHT)
        self.setToolTip(str(info.get("path") or ""))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(5)

        name = QLabel(str(info.get("name") or self.plugin_id))
        font = QFont()
        font.setPointSizeF(11.0)
        font.setBold(True)
        name.setFont(font)
        outer.addWidget(name)

        ident = QLabel(f"{self.plugin_id}  v{info.get('version') or '?'}")
        ident.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 10px;")
        outer.addWidget(ident)

        description = QLabel(str(info.get("description") or "（没有填写描述）"))
        description.setWordWrap(True)
        description.setFixedHeight(34)
        description.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        description.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        outer.addWidget(description)

        actions = list(info.get("actions") or [])
        triggers = list(info.get("triggers") or [])
        counts = QLabel(f"{len(actions)} 个动作 · {len(triggers)} 个触发器")
        counts.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        outer.addWidget(counts)

        if info.get("load_error"):
            outer.addWidget(CustomModuleRow._meta("加载失败", True))
        elif used_by:
            outer.addWidget(
                CustomModuleRow._meta(f"{len(used_by)} 条流程在用", False, warn=True)
            )
        else:
            outer.addWidget(CustomModuleRow._meta("还没有流程用它", False))

        outer.addStretch(1)

        # 缺依赖时在按钮行**上面**单独占一行 —— 方砖只有 302 宽，四个短按钮已经排满，
        # 再挤一个进去谁也看不清。
        self.install_button: QPushButton | None = None
        missing = list(info.get("missing_dependencies") or [])
        if missing:
            install = QPushButton("安装依赖")
            install.setStyleSheet(_INSTALL_STYLE)
            install.setToolTip("缺：" + "、".join(missing) + "\n点一下在当前环境里装上")
            install.clicked.connect(
                lambda: self.installRequested.emit(self.plugin_id, missing)
            )
            outer.addWidget(install)
            self.install_button = install

        buttons = QHBoxLayout()
        buttons.setSpacing(theme.ROW_BUTTON_SPACING)
        row_buttons: list[QPushButton] = []
        for text, tip, signal in (
            ("代码", "在右侧打开这个模块的 main.py", self.editRequested),
            ("改名", "只改显示名；标识和目录不动", self.renameRequested),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.setStyleSheet(_TILE_BUTTON_STYLE)
            button.clicked.connect(lambda _=False, sig=signal: sig.emit(self.plugin_id))
            row_buttons.append(button)

        # 「位置」发的是**目录路径**，不是模块标识 —— 和上面三个不是一回事。
        # 之前它跟着循环一起发 plugin_id，点下去只会得到一句"目录不存在：fgo"。
        # 列表行那边一直是对的，所以只有网格视图坏。
        reveal = QPushButton("位置")
        reveal.setToolTip("在资源管理器里打开这个模块的文件夹")
        reveal.setStyleSheet(_TILE_BUTTON_STYLE)
        reveal.clicked.connect(lambda: self.revealRequested.emit(self.path))
        row_buttons.append(reveal)

        self.delete_button = QPushButton("删除")
        self.delete_button.setToolTip("连同这个模块的文件夹一起删除")
        self.delete_button.setStyleSheet(_TILE_DELETE_STYLE)
        self.delete_button.clicked.connect(
            lambda: self.deleteRequested.emit(self.plugin_id)
        )
        row_buttons.append(self.delete_button)

        # 四个按钮**等宽平铺整行**。删除不再单拎出去 —— 它跟另外三个并排，红色的描边
        # 已经足够区分，再拉开距离反而让这一行看着断成两截。
        for button in row_buttons:
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setFixedHeight(_TILE_BUTTON_HEIGHT)
            buttons.addWidget(button, 1)
        outer.addLayout(buttons)


class CustomModuleView(QWidget):
    """自定义模块页整体。"""

    deleteRequested = Signal(str)
    editRequested = Signal(str)
    renameRequested = Signal(str)
    revealRequested = Signal(str)
    openFolderRequested = Signal()
    #: 某条模块缺依赖要装（模块 id, pip 要求列表）—— 逐条转发，不做页面级的汇总按钮
    installRequested = Signal(str, list)

    def __init__(self, registry: Any, workflows_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.registry = registry
        self.workflows_dir = Path(workflows_dir)
        self._rows: dict[str, CustomModuleRow] = {}
        self._tiles: dict[str, ModuleTile] = {}
        self._used: dict[str, list[str]] = {}
        self._catalog: list[dict[str, Any]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)

        heading = QLabel("我的模块")
        font = QFont()
        font.setPointSizeF(14.0)
        font.setBold(True)
        heading.setFont(font)
        header.addWidget(heading)

        self.summary = QLabel()
        self.summary.setStyleSheet(f"color: {theme.NODE_SUBTITLE};")
        header.addWidget(self.summary)
        header.addStretch(1)

        self.folder_button = QPushButton("打开模块目录")
        self.folder_button.setToolTip("自己写的插件放进这个目录就会出现在这里")
        self.folder_button.clicked.connect(self.openFolderRequested)
        header.addWidget(self.folder_button)

        self.toggle = ViewModeToggle()
        self.toggle.changed.connect(self.set_view_mode)
        header.addWidget(self.toggle)
        layout.addLayout(header)

        self.hint = QLabel(
            "这里列的是你自己的模块 —— 用底部「AI 对话」生成的，或者手写一个插件文件夹"
            "放进 plugins/ 的。内置模块在「插件」页，删不掉。"
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        layout.addWidget(self.hint)

        self.wall = CardWall(self._make_tile, self._make_row)
        self.wall.hint_text = self._empty_hint
        layout.addWidget(self.wall, 1)

    # -- 数据 -----------------------------------------------------------------

    def reload(self) -> None:
        """重新扫描并重建。"""
        self._rows.clear()
        self._tiles.clear()

        catalog = {
            str(entry["id"]): entry
            for entry in self.registry.catalog()
            if not self.registry.is_builtin(str(entry["id"]))
        }
        # 加载失败的模块 catalog 里也有（带 load_error），但没被发现的坏目录不会出现 ——
        # 补上目录扫描，免得一个坏掉的模块连删都删不掉。
        for plugin_id, manifest in self.registry.manifests.items():
            if self.registry.is_builtin(plugin_id) or plugin_id in catalog:
                continue
            catalog[plugin_id] = {
                "id": plugin_id,
                "name": manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "path": str(manifest.path),
                "actions": [],
                "triggers": [],
                "load_error": "（没能加载接口）",
            }

        self._used = {
            plugin_id: workflows_using(self.workflows_dir, plugin_id)
            for plugin_id in catalog
        }
        self._catalog = [catalog[pid] for pid in sorted(catalog)]
        self.wall.set_items(self._catalog)

        builtin_count = len(self.registry.manifests) - len(catalog)
        self.summary.setText(
            f"共 {len(catalog)} 个" + (f"（另有 {builtin_count} 个内置模块）" if builtin_count else "")
        )

        missing = self.missing_requirements()
        if missing:
            self.summary.setText(self.summary.text() + f" · 缺 {len(missing)} 个依赖")

    def missing_requirements(self) -> list[str]:
        """所有自建模块缺的依赖，去重后按声明顺序返回。汇总信息用，按钮在每条卡片上。"""
        out: list[str] = []
        seen: set[str] = set()
        for info in self._catalog:
            for requirement in info.get("missing_dependencies") or []:
                key = requirement.lower()
                if key not in seen:
                    seen.add(key)
                    out.append(requirement)
        return out

    def set_install_enabled(self, enabled: bool) -> None:
        """安装期间把每条卡片上的按钮禁掉 —— 同时开几个 pip 只会把环境搞乱。"""
        for widget in list(self._rows.values()) + list(self._tiles.values()):
            button = getattr(widget, "install_button", None)
            if button is not None:
                button.setEnabled(enabled)
                button.setText("安装依赖" if enabled else "正在安装…")

    # -- 工厂 -----------------------------------------------------------------

    def _make_tile(self, info: dict[str, Any]) -> QWidget:
        tile = ModuleTile(info, self._used.get(str(info["id"]), []))
        tile.deleteRequested.connect(self.deleteRequested)
        tile.editRequested.connect(self.editRequested)
        tile.renameRequested.connect(self.renameRequested)
        tile.revealRequested.connect(self.revealRequested)
        tile.installRequested.connect(self.installRequested)
        self._tiles[str(info["id"])] = tile
        return tile

    def _make_row(self, info: dict[str, Any]) -> QWidget:
        row = CustomModuleRow(info, self._used.get(str(info["id"]), []))
        row.deleteRequested.connect(self.deleteRequested)
        row.editRequested.connect(self.editRequested)
        row.renameRequested.connect(self.renameRequested)
        row.revealRequested.connect(self.revealRequested)
        row.installRequested.connect(self.installRequested)
        self._rows[str(info["id"])] = row
        return row

    def _empty_hint(self) -> str:
        # 只给文字。居中交给 CardWall —— 它把提示放在栈外面，那里天然占满整行。
        return (
            "还没有自定义模块。\n用底部「AI 对话」描述你要什么，或者点左侧「新建模块」自己写。"
        )

    def set_view_mode(self, mode: str) -> None:
        self.wall.set_view_mode(mode)
        self.toggle.set_mode(mode)

    @property
    def view_mode(self) -> str:
        return self.wall.view_mode

    def row_for(self, plugin_id: str) -> CustomModuleRow | None:
        return self._rows.get(plugin_id)

    def tile_for(self, plugin_id: str) -> ModuleTile | None:
        return self._tiles.get(plugin_id)

    def set_delete_enabled(self, enabled: bool) -> None:
        """运行中不许删 —— 插件进程正被引擎用着。"""
        for widget in list(self._rows.values()) + list(self._tiles.values()):
            widget.delete_button.setEnabled(enabled)
