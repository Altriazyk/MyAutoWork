"""端口。

两种端口长得不一样，这不是装饰：

- **执行端口**是三角，表示"控制流经过这里"
- **数据端口**是圆点，颜色 = 数据类型

用户扫一眼画布就能分清哪根线管顺序、哪根线管取值。这正是双线模型最需要的视觉区分 ——
如果两种边长得一样，画布很快会变成一团看不出先后的面条。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem

from .. import theme
from ..constants import DIR_OUT, KIND_EXEC

if TYPE_CHECKING:  # pragma: no cover
    from .node_item import NodeItem


class PortItem(QGraphicsItem):
    """节点边缘的一个连接点。"""

    def __init__(
        self,
        owner: "NodeItem",
        name: str,
        direction: str,
        kind: str,
        *,
        data_type: str = "any",
        label: str = "",
        parent: QGraphicsItem | None = None,
    ) -> None:
        super().__init__(parent)
        self.owner = owner
        self.name = name
        self.direction = direction
        self.kind = kind
        self.data_type = data_type
        self.label = label
        self.connected = False
        self._hovered = False

        self.setAcceptHoverEvents(True)
        self.setZValue(theme.Z_PORT)
        self.setToolTip(self._tooltip())

    # -- 绘制 ----------------------------------------------------------------

    def boundingRect(self) -> QRectF:
        r = theme.PORT_HIT_RADIUS
        return QRectF(-r, -r, 2 * r, 2 * r)

    def shape(self) -> QPainterPath:
        # 用比可见图形更大的圆做命中区 —— 5 像素的点直接点很难点中。
        path = QPainterPath()
        path.addEllipse(QPointF(0, 0), theme.PORT_HIT_RADIUS, theme.PORT_HIT_RADIUS)
        return path

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = theme.PORT_RADIUS
        base = QColor(theme.port_color(self.kind, self.data_type))
        filled = self.connected or self._hovered
        color = base.lighter(135) if self._hovered else base

        painter.setPen(QPen(color, 1.8))
        painter.setBrush(color if filled else Qt.BrushStyle.NoBrush)

        if self.kind == KIND_EXEC:
            # 所有执行端口都指向右侧 = 控制流的行进方向。
            path = QPainterPath()
            path.moveTo(-radius * 0.8, -radius)
            path.lineTo(radius * 1.05, 0.0)
            path.lineTo(-radius * 0.8, radius)
            path.closeSubpath()
            painter.drawPath(path)
        else:
            painter.drawEllipse(QPointF(0, 0), radius, radius)

    # -- 交互 ----------------------------------------------------------------

    def hoverEnterEvent(self, event: Any) -> None:
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: Any) -> None:
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            scene = self.scene()
            if scene is not None and hasattr(scene, "begin_connection"):
                # 接受事件 = 抓住鼠标，后续 move/release 都归这里，
                # 于是不会误触发父节点的拖动。
                scene.begin_connection(self)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        scene = self.scene()
        if scene is not None and getattr(scene, "_connecting_from", None) is not None:
            scene.update_connection(event.scenePos())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        scene = self.scene()
        if scene is not None and getattr(scene, "_connecting_from", None) is not None:
            scene.finish_connection(event.scenePos())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- 状态 ----------------------------------------------------------------

    def set_connected(self, connected: bool) -> None:
        if connected != self.connected:
            self.connected = connected
            self.update()

    def is_output(self) -> bool:
        return self.direction == DIR_OUT

    def endpoint_key(self) -> tuple[str, str]:
        return (self.owner.node_id, self.name)

    def _tooltip(self) -> str:
        if self.kind == KIND_EXEC:
            role = "执行出口" if self.is_output() else "执行入口"
            return f"{role}：{self.name}"
        role = "输出" if self.is_output() else "输入"
        label = f"{self.label}（{self.name}）" if self.label else self.name
        return f"{role}：{label} · 类型 {self.data_type}"

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"<Port {self.owner.node_id}.{self.name} {self.direction}/{self.kind}>"
