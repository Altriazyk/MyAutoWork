"""画布场景：节点、连线、连线的合法性判定。

这里是**双线模型在界面上的执法者**。引擎运行时假定"连出来的东西是合法的"，所以
所有约束都必须在连线的那一刻挡住：

- 执行端口只能连执行端口，数据端口只能连数据端口
- 数据类型必须兼容（``myautowork.fields.compatible``）
- 一个数据输入只能有一条入线（再连就替换，并告诉用户替换了哪条）
- 不能连到自己

报错要给出**具体原因**，而不是简单拒绝。用户连不上却不知道为什么，是节点编辑器最
劝退的体验。
"""

from __future__ import annotations

from typing import Any, Iterable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsPathItem, QGraphicsScene

from kernel.graph import Edge, Node, ParamValue, Workflow
from myautowork.fields import compatible

from .. import theme
from ..constants import DIR_IN, DIR_OUT, EXEC_IN_PORT, KIND_DATA, KIND_EXEC
from .edge_item import EdgeItem
from .node_item import NodeItem
from .port_item import PortItem

#: 插件缺失时的占位接口。工作流引用了没装的插件也要能打开 ——
#: 报错说"这个插件没装"，比整张图打不开有用得多。
MISSING_SPEC = {
    "name": "未知动作",
    "category": "通用",
    "kind": "action",
    "inputs": {},
    "outputs": {},
    "missing": True,
}


class WorkflowScene(QGraphicsScene):
    workflowChanged = Signal()
    nodeSelected = Signal(object)  # NodeItem | None
    statusMessage = Signal(str)

    def __init__(self, registry: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.registry = registry
        self.workflow_id = "workflow"
        self.workflow_name = "未命名工作流"
        self.workflow_description = ""
        self.workflow_variables: dict[str, Any] = {}

        self._node_items: dict[str, NodeItem] = {}
        self._edge_items: list[EdgeItem] = []
        self._connecting_from: PortItem | None = None
        self._preview: QGraphicsPathItem | None = None
        self._loading = False

        self.setBackgroundBrush(QColor(theme.BACKGROUND))
        self.selectionChanged.connect(self._on_selection_changed)
        self._update_scene_rect()

    # -- 查询 ----------------------------------------------------------------

    @property
    def node_items(self) -> dict[str, NodeItem]:
        return self._node_items

    @property
    def edge_items(self) -> list[EdgeItem]:
        return list(self._edge_items)

    def node_item(self, node_id: str) -> NodeItem | None:
        return self._node_items.get(node_id)

    def selected_node(self) -> NodeItem | None:
        for item in self.selectedItems():
            if isinstance(item, NodeItem):
                return item
        return None

    def is_empty(self) -> bool:
        return not self._node_items

    def _port_at(self, pos: QPointF) -> PortItem | None:
        for item in self.items(pos):
            if isinstance(item, PortItem):
                return item
        return None

    def _edges_touching(self, node_id: str) -> Iterable[EdgeItem]:
        for edge in self._edge_items:
            if edge.src_port.owner.node_id == node_id or edge.dst_port.owner.node_id == node_id:
                yield edge

    def _data_edge_into(self, port: PortItem) -> EdgeItem | None:
        for edge in self._edge_items:
            if edge.kind == KIND_DATA and edge.dst_port is port:
                return edge
        return None

    def upstream_description(self, node_id: str, port: str) -> str | None:
        """给属性面板用：这个输入的上游是谁。"""
        for edge in self._edge_items:
            if edge.kind == KIND_DATA and edge.dst_port.owner.node_id == node_id and edge.dst_port.name == port:
                return f"{edge.src_port.owner.node_id}.{edge.src_port.name}"
        return None

    # -- 增删 ----------------------------------------------------------------

    def _next_node_id(self) -> str:
        index = 1
        while f"n{index}" in self._node_items:
            index += 1
        return f"n{index}"

    def add_node(
        self,
        plugin: str,
        action: str,
        pos: QPointF,
        *,
        node_id: str | None = None,
        params: dict[str, ParamValue] | None = None,
        title: str | None = None,
        disabled: bool = False,
        timeout: float | None = None,
        spec: dict[str, Any] | None = None,
    ) -> NodeItem:
        if spec is None:
            spec = self.registry.node_spec(plugin, action)
        missing = spec is None
        if missing:
            spec = dict(MISSING_SPEC, name=f"未知动作：{action}")

        node_id = node_id or self._next_node_id()
        item = NodeItem(
            node_id,
            plugin,
            action,
            spec,
            params,
            pos=pos,
            title=title,
            disabled=disabled,
            missing=missing,
            timeout=timeout,
        )
        self.addItem(item)
        self._node_items[node_id] = item
        self._update_scene_rect()
        if not self._loading:
            self.workflowChanged.emit()
        return item

    def add_edge(self, src_port: PortItem, dst_port: PortItem, *, emit: bool = True) -> EdgeItem:
        edge = EdgeItem(src_port, dst_port, KIND_EXEC if src_port.kind == KIND_EXEC else KIND_DATA)
        self.addItem(edge)
        self._edge_items.append(edge)
        if emit and not self._loading:
            self.workflowChanged.emit()
        return edge

    def _remove_edge(self, edge: EdgeItem) -> None:
        if edge in self._edge_items:
            self._edge_items.remove(edge)
            edge.src_port.set_connected(self._has_other_edge(edge.src_port))
            edge.dst_port.set_connected(self._has_other_edge(edge.dst_port))
            self.removeItem(edge)

    def _has_other_edge(self, port: PortItem) -> bool:
        return any(
            edge.src_port is port or edge.dst_port is port
            for edge in self._edge_items
        )

    def remove_selected(self) -> None:
        nodes = [item for item in self.selectedItems() if isinstance(item, NodeItem)]
        edges = [item for item in self.selectedItems() if isinstance(item, EdgeItem)]

        for node in nodes:
            for edge in list(self._edges_touching(node.node_id)):
                self._remove_edge(edge)
        for edge in edges:
            self._remove_edge(edge)

        for node in nodes:
            self._node_items.pop(node.node_id, None)
            self.removeItem(node)

        if nodes or edges:
            self._update_scene_rect()
            self.statusMessage.emit(f"已删除 {len(nodes)} 个节点、{len(edges)} 条连线")
            self.workflowChanged.emit()

    def clear_selection(self) -> None:
        self.clearSelection()

    # -- 连线合法性 -----------------------------------------------------------

    def can_connect(self, a: PortItem, b: PortItem) -> tuple[bool, str]:
        if a is b:
            return False, "不能连到端口自己"
        if a.direction == b.direction:
            side = "输出" if a.direction == DIR_OUT else "输入"
            return False, f"两个都是{side}端口，连线必须从输出连到输入"
        if a.kind != b.kind:
            return False, "执行端口只能连执行端口，数据端口只能连数据端口"
        if a.owner is b.owner:
            return False, "不能连到同一个节点上"

        out_port, in_port = (a, b) if a.is_output() else (b, a)

        if out_port.kind == KIND_DATA and not compatible(out_port.data_type, in_port.data_type):
            return False, (
                f"类型不兼容：{out_port.owner.node_id}.{out_port.name} 是 {out_port.data_type}，"
                f"但 {in_port.owner.node_id}.{in_port.name} 需要 {in_port.data_type}"
            )

        for edge in self._edge_items:
            if edge.src_port is out_port and edge.dst_port is in_port:
                return False, "这两个端口已经连过了"
        return True, ""

    # -- 拖拽连线 -------------------------------------------------------------

    def begin_connection(self, port: PortItem) -> None:
        self._connecting_from = port
        self._preview = QGraphicsPathItem()
        self._preview.setZValue(theme.Z_EDGE_PREVIEW)
        pen = QPen(QColor(theme.port_color(port.kind, port.data_type)), 2.0)
        pen.setStyle(Qt.PenStyle.DashLine)
        self._preview.setPen(pen)
        self.addItem(self._preview)
        self.update_connection(port.scenePos())

    def update_connection(self, pos: QPointF) -> None:
        if self._connecting_from is None or self._preview is None:
            return
        src = self._connecting_from.scenePos()
        span = max(40.0, abs(pos.x() - src.x()) * 0.5)
        if self._connecting_from.is_output():
            control1 = QPointF(src.x() + span, src.y())
            control2 = QPointF(pos.x() - span, pos.y())
        else:
            control1 = QPointF(src.x() - span, src.y())
            control2 = QPointF(pos.x() + span, pos.y())
        path = QPainterPath(src)
        path.cubicTo(control1, control2, pos)
        self._preview.setPath(path)

    def finish_connection(self, pos: QPointF) -> None:
        source = self._connecting_from
        target = self._port_at(pos)
        self._clear_preview()
        if source is None or target is None:
            return

        ok, reason = self.can_connect(source, target)
        if not ok:
            self.statusMessage.emit(f"连不上：{reason}")
            return

        out_port, in_port = (source, target) if source.is_output() else (target, source)

        if out_port.kind == KIND_DATA:
            existing = self._data_edge_into(in_port)
            if existing is not None:
                # 替换而不是拒绝：一个输入只能有一条数据线，替换更符合直觉。
                self.statusMessage.emit(
                    f"{in_port.owner.node_id}.{in_port.name} 原有的连线来自 "
                    f"{existing.src_port.owner.node_id}.{existing.src_port.name}，已替换"
                )
                self._remove_edge(existing)

        self.add_edge(out_port, in_port)
        self.statusMessage.emit(
            f"已连接 {out_port.owner.node_id}.{out_port.name} -> {in_port.owner.node_id}.{in_port.name}"
        )

    def _clear_preview(self) -> None:
        if self._preview is not None:
            self.removeItem(self._preview)
            self._preview = None
        self._connecting_from = None

    # -- 运行态 --------------------------------------------------------------

    def reset_run_state(self) -> None:
        for item in self._node_items.values():
            item.clear_run_state()
        for edge in self._edge_items:
            edge.set_flow_state("")

    def mark_node_started(self, node_id: str) -> None:
        item = self._node_items.get(node_id)
        if item is not None:
            item.set_run_state("running")
        for edge in self._edge_items:
            if edge.src_port.owner.node_id == node_id:
                edge.set_flow_state("running")

    def mark_node_finished(self, node_id: str, *, duration_ms: int, ok: bool = True) -> None:
        item = self._node_items.get(node_id)
        if item is not None:
            item.set_run_state("success" if ok else "failed", duration_ms=duration_ms)
        for edge in self._edge_items:
            if edge.src_port.owner.node_id == node_id:
                edge.set_flow_state("done" if ok else "failed")

    def mark_node_failed(self, node_id: str, message: str) -> None:
        item = self._node_items.get(node_id)
        if item is not None:
            item.set_run_state("failed", message=message)
        for edge in self._edge_items:
            if edge.src_port.owner.node_id == node_id:
                edge.set_flow_state("failed")

    def mark_node_skipped(self, node_id: str) -> None:
        item = self._node_items.get(node_id)
        if item is not None:
            item.set_run_state("skipped")

    def refresh_edges_for(self, node_id: str) -> None:
        for edge in self._edges_touching(node_id):
            edge.update_path()

    # -- 数据同步 -------------------------------------------------------------

    def set_param(self, node_id: str, port: str, param: ParamValue) -> None:
        item = self._node_items.get(node_id)
        if item is None:
            return
        item.params[port] = param
        self.workflowChanged.emit()

    def _on_selection_changed(self) -> None:
        self.nodeSelected.emit(self.selected_node())

    def _update_scene_rect(self) -> None:
        bounds = self.itemsBoundingRect()
        if bounds.isNull() or bounds.width() < 1.0:
            bounds = QRectF(0.0, 0.0, 1200.0, 800.0)
        self.setSceneRect(bounds.adjusted(-260.0, -220.0, 260.0, 260.0))

    # -- 与 Workflow 互转 -----------------------------------------------------

    def to_workflow(self) -> Workflow:
        nodes: dict[str, Node] = {}
        for node_id, item in self._node_items.items():
            nodes[node_id] = Node(
                id=node_id,
                plugin=item.plugin,
                action=item.action,
                pos=(item.pos().x(), item.pos().y()),
                params=dict(item.params),
                title=item.custom_title,
                disabled=item.disabled,
                timeout=item.timeout,
            )

        edges: list[Edge] = []
        for edge in self._edge_items:
            edges.append(
                Edge(
                    src=edge.src_port.owner.node_id,
                    dst=edge.dst_port.owner.node_id,
                    kind=edge.kind,
                    src_port=edge.src_port.name,
                    dst_port=edge.dst_port.name if edge.kind != KIND_EXEC else "",
                )
            )

        return Workflow(
            id=self.workflow_id,
            name=self.workflow_name,
            description=self.workflow_description,
            nodes=nodes,
            edges=edges,
            variables=dict(self.workflow_variables),
        )

    def load_workflow(self, workflow: Workflow) -> list[str]:
        """把工作流铺到画布上。返回无法还原的部分（缺失插件、消失的端口）。"""
        warnings: list[str] = []
        self._loading = True
        try:
            self._connecting_from = None
            self._preview = None
            self._node_items.clear()
            self._edge_items.clear()
            super().clear()  # 必须在清空 Python 引用之后调用，否则会拿到已析构的对象

            self.workflow_id = workflow.id
            self.workflow_name = workflow.name
            self.workflow_description = workflow.description
            self.workflow_variables = dict(workflow.variables)

            for node in workflow.nodes.values():
                item = self.add_node(
                    node.plugin,
                    node.action,
                    QPointF(node.pos[0], node.pos[1]),
                    node_id=node.id,
                    params=dict(node.params),
                    title=node.title,
                    disabled=node.disabled,
                    timeout=node.timeout,
                )
                if item.missing:
                    warnings.append(f"节点 {node.id}：插件 {node.plugin} 未安装或动作 {node.action} 不存在")

            for edge in workflow.edges:
                src_item = self._node_items.get(edge.src)
                dst_item = self._node_items.get(edge.dst)
                if src_item is None or dst_item is None:
                    warnings.append(f"连线 {edge.src} -> {edge.dst}：端点节点不存在")
                    continue
                if edge.kind == KIND_EXEC:
                    # 执行边在工作流里不记录目标端口（引擎不关心顺序端口叫什么都行），
                    # 但画布上必须落到具体的执行入口上。
                    src_port = src_item.port(edge.src_port, DIR_OUT)
                    dst_port = dst_item.port(EXEC_IN_PORT, DIR_IN)
                else:
                    src_port = src_item.port(edge.src_port, DIR_OUT)
                    dst_port = dst_item.port(edge.dst_port, DIR_IN)
                if src_port is None or dst_port is None:
                    warnings.append(
                        f"连线 {edge.src}.{edge.src_port} -> {edge.dst}.{edge.dst_port}："
                        "端口不存在（插件接口可能变了）"
                    )
                    continue
                self.add_edge(src_port, dst_port, emit=False)
        finally:
            self._loading = False

        self._update_scene_rect()
        self.workflowChanged.emit()
        return warnings
