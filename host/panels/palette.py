"""左侧模块面板。

数据全部来自 ``registry.catalog()``，也就是插件自己声明的接口。这里没有任何
"某某插件"的特判 —— 新装一个插件，它就出现在树里。

拖到画布上会带一段自定义 MIME（``plugin\\naction``），画布负责落成节点。拖拽和
双击都要能用：双击是稳妥路径，拖拽是手感。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..constants import NODE_MIME


class _NodeTree(QTreeWidget):
    """只有叶子节点（动作）才可拖。"""

    def mimeData(self, items: Any) -> QMimeData | None:  # noqa: N802 - Qt 重写
        for item in items:
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data:
                mime = QMimeData()
                mime.setData(NODE_MIME, f"{data[0]}\n{data[1]}".encode("utf-8"))
                return mime
        return None


class PalettePanel(QWidget):
    """按 分类 → 插件 → 动作 组织的模块树。"""

    nodeActivated = Signal(str, str)  # plugin, action

    def __init__(self, registry: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.registry = registry

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索模块…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.tree = _NodeTree()
        self.tree.setHeaderHidden(True)
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setUniformRowHeights(True)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.tree, 1)

        self.summary = QLabel()
        self.summary.setStyleSheet(f"color: {theme.NODE_SUBTITLE}; font-size: 11px;")
        layout.addWidget(self.summary)

        self.rebuild()

    # -- 构建 ----------------------------------------------------------------

    def rebuild(self) -> None:
        self.tree.clear()
        catalog = self.registry.catalog()

        by_category: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for plugin in catalog:
            entries = list(plugin.get("triggers") or []) + list(plugin.get("actions") or [])
            for spec in entries:
                category = spec.get("category") or plugin.get("category") or "通用"
                by_category.setdefault(str(category), []).append((plugin, spec))

        total = 0
        for category in sorted(by_category):
            entries = by_category[category]
            category_item = QTreeWidgetItem([f"{category}（{len(entries)}）"])
            category_font = QFont()
            category_font.setBold(True)
            category_item.setFont(0, category_font)
            category_item.setForeground(0, QBrush(QColor(theme.category_color(category))))
            category_item.setFlags(Qt.ItemFlag.ItemIsEnabled)

            by_plugin: dict[str, list[dict[str, Any]]] = {}
            for plugin, spec in entries:
                by_plugin.setdefault(plugin["id"], []).append(spec)

            for plugin_id in sorted(by_plugin):
                plugin = next(p for p, _ in entries if p["id"] == plugin_id)
                plugin_item = QTreeWidgetItem([str(plugin.get("name") or plugin_id)])
                plugin_item.setToolTip(0, f"{plugin_id}\n{plugin.get('description', '')}")
                plugin_item.setForeground(0, QBrush(QColor(theme.NODE_TEXT)))

                for spec in sorted(by_plugin[plugin_id], key=lambda s: s["id"]):
                    is_trigger = spec.get("kind") == "trigger"
                    badge = "触发" if is_trigger else "动作"
                    label = str(spec.get("name") or spec["id"])
                    action_item = QTreeWidgetItem([f"{label}   · {badge}"])
                    action_item.setData(0, Qt.ItemDataRole.UserRole, (plugin_id, spec["id"]))
                    action_item.setToolTip(
                        0,
                        f"{plugin_id}/{spec['id']}\n{spec.get('description', '')}\n\n双击或拖到画布上添加",
                    )
                    accent = QColor(theme.category_color(str(spec.get("category") or category)))
                    action_item.setForeground(0, QBrush(accent))
                    plugin_item.addChild(action_item)
                    total += 1

                category_item.addChild(plugin_item)
            self.tree.addTopLevelItem(category_item)

        self.tree.expandAll()
        broken = len(getattr(self.registry, "problems", []) or [])
        note = f"，{broken} 个插件加载失败" if broken else ""
        self.summary.setText(f"{len(catalog)} 个插件 · {total} 个模块{note}")
        self._apply_filter(self.search.text())

    # -- 过滤 ----------------------------------------------------------------

    def _apply_filter(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for index in range(self.tree.topLevelItemCount()):
            category_item = self.tree.topLevelItem(index)
            visible = self._filter_item(category_item, needle)
            category_item.setHidden(not visible)

    def _filter_item(self, item: QTreeWidgetItem, needle: str) -> bool:
        if not needle:
            item.setHidden(False)
            for index in range(item.childCount()):
                self._filter_item(item.child(index), needle)
            return True

        child_visible = False
        for index in range(item.childCount()):
            if self._filter_item(item.child(index), needle):
                child_visible = True

        haystack = f"{item.text(0)} {item.toolTip(0)}".lower()
        self_match = needle in haystack
        visible = self_match or child_visible
        item.setHidden(not visible)
        if child_visible and item.childCount():
            item.setExpanded(True)
        return visible

    # -- 交互 ----------------------------------------------------------------

    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            self.nodeActivated.emit(str(data[0]), str(data[1]))
