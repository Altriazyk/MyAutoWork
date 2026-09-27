"""连线。

两种边长得完全不同，这是刻意的：

- **执行边**：灰白实线 + 箭头，粗一点。它表示"顺序"。
- **数据边**：按数据类型着色的曲线，没有箭头。它表示"取值"。

如果两种边长得一样，画布很快会变成一堆看不出先后的面条。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPainterPathStroker, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem

from .. import theme
from ..constants import KIND_EXEC, PORT_ERROR
from .port_item import PortItem

#: 连线的命中宽度。按 1 像素宽的曲线去点会点到崩溃。
SHAPE_WIDTH = 11.0


class EdgeItem(QGraphicsPathItem):
    """连接一个输出端口和一个输入端口。"""

    def __init__(self, src_port: PortItem, dst_port: PortItem, kind: str) -> None:
        super().__init__()
        self.src_port = src_port
        self.dst_port = dst_port
        self.kind = kind
        self.flow_state: str = ""

        self.setZValue(theme.Z_EDGE)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)

        src_port.set_connected(True)
        dst_port.set_connected(True)
        self.update_path()

    # -- 几何 ----------------------------------------------------------------

    def update_path(self) -> None:
        src = self.src_port.scenePos()
        dst = self.dst_port.scenePos()
        # 提前收住，把箭头露出来（端口圆点画在连线之上）。
        end = QPointF(dst.x() - theme.PORT_RADIUS - 3.0, dst.y())

        # 控制点始终水平外凸：终点切线方向恒为"向右"，
        # 所以即使数据边倒流（目标在源的左边），箭头也一致地指向输入端口。
        span = max(48.0, abs(end.x() - src.x()) * 0.5)
        path = QPainterPath(src)
        path.cubicTo(
            QPointF(src.x() + span, src.y()),
            QPointF(end.x() - span, end.y()),
            end,
        )
        self.setPath(path)

    def boundingRect(self):  # noqa: ANN201 - Qt 重写
        return super().boundingRect().adjusted(-8.0, -8.0, 8.0, 8.0)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(SHAPE_WIDTH)
        return stroker.createStroke(self.path())

    # -- 外观 ----------------------------------------------------------------

    @property
    def is_exec(self) -> bool:
        return self.kind == KIND_EXEC

    def color(self) -> str:
        if self.flow_state == "running":
            return theme.STATE_RUNNING
        if self.flow_state == "done":
            return theme.STATE_SUCCESS
        if self.flow_state == "failed":
            return theme.STATE_FAILED
        if self.is_exec:
            return theme.EXEC_ERROR_COLOR if self.src_port.name == PORT_ERROR else theme.EXEC_COLOR
        return theme.port_color("data", self.src_port.data_type)

    def set_flow_state(self, state: str) -> None:
        if state != self.flow_state:
            self.flow_state = state
            self.update()

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = QColor(self.color())
        width = theme.EDGE_WIDTH_EXEC if self.is_exec else theme.EDGE_WIDTH_DATA

        if self.isSelected():
            width += 1.4
            color = color.lighter(125)
        elif self.flow_state:
            width += 0.8

        pen = QPen(color, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if self.src_port.name == PORT_ERROR and not self.flow_state:
            pen.setStyle(Qt.PenStyle.DashLine)  # 错误出口用虚线，一眼能认出来
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())

        if self.is_exec:
            self._paint_arrow(painter, color)

    def _paint_arrow(self, painter: QPainter, color: QColor) -> None:
        end = self.path().pointAtPercent(1.0)
        tip = QPointF(end.x() + theme.PORT_RADIUS + 2.0, end.y())
        size = theme.EDGE_ARROW_SIZE

        arrow = QPainterPath()
        arrow.moveTo(tip)
        arrow.lineTo(tip.x() - size, tip.y() - size * 0.52)
        arrow.lineTo(tip.x() - size, tip.y() + size * 0.52)
        arrow.closeSubpath()

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPath(arrow)

    # -- 交互 ----------------------------------------------------------------

    def hoverEnterEvent(self, event: Any) -> None:
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: Any) -> None:
        self.update()
        super().hoverLeaveEvent(event)

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"<EdgeItem {self.src_port.owner.node_id}.{self.src_port.name}"
            f" -> {self.dst_port.owner.node_id}.{self.dst_port.name} {self.kind}>"
        )
