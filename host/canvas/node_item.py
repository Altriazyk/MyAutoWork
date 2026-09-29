"""节点图元。

节点长什么样，完全由插件的接口声明决定：

- 行数 = 输入端口数 / 输出端口数里多的那个
- 高度 = 表头 + 行数 × 行高
- 端口颜色 = 数据类型
- 表头颜色 = 插件的 category

所以这里没有任何"某某插件"的判断。加插件，节点自动就对了。
"""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from kernel.graph import ParamValue, branch_names

from .. import theme
from ..constants import DIR_IN, DIR_OUT, EXEC_IN_PORT, KIND_DATA, KIND_EXEC, PORT_ERROR, PORT_SUCCESS
from .port_item import PortItem


def _top_rounded_path(width: float, height: float, radius: float) -> QPainterPath:
    """只有上面两个角是圆的矩形，用作表头。"""
    path = QPainterPath()
    path.moveTo(0.0, height)
    path.lineTo(0.0, radius)
    path.quadTo(0.0, 0.0, radius, 0.0)
    path.lineTo(width - radius, 0.0)
    path.quadTo(width, 0.0, width, radius)
    path.lineTo(width, height)
    path.closeSubpath()
    return path


class NodeItem(QGraphicsObject):
    """画布上的一个节点。"""

    paramsChanged = Signal(str)  # node_id

    def __init__(
        self,
        node_id: str,
        plugin: str,
        action: str,
        spec: Mapping[str, Any],
        params: Mapping[str, ParamValue] | None = None,
        *,
        pos: QPointF | None = None,
        title: str | None = None,
        disabled: bool = False,
        missing: bool = False,
        timeout: float | None = None,
    ) -> None:
        super().__init__()
        self.node_id = node_id
        self.plugin = plugin
        self.action = action
        self.spec: dict[str, Any] = dict(spec)
        self.params: dict[str, ParamValue] = dict(params or {})
        self.custom_title = title
        self.disabled = disabled
        self.missing = missing
        self.timeout = timeout

        self.run_state: str = ""
        self.run_duration_ms: int | None = None
        self.run_message: str | None = None
        #: 上一次运行这个节点输出了什么。属性面板靠它显示"它到底算出了什么"。
        #: 只在画布上跑过的节点才有 —— 从流程列表跑的不进这里（那时画布上可能是另一条流程）。
        self.last_outputs: dict[str, Any] = {}
        self.last_error: str = ""

        self.input_ports: dict[str, PortItem] = {}
        self.output_ports: dict[str, PortItem] = {}
        #: 上一次建端口时用的分支名。用来判断"参数改了之后要不要重建"。
        self._branches: list[str] = []

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setZValue(theme.Z_NODE)
        self.setAcceptHoverEvents(True)

        self._build_ports()
        if pos is not None:
            self.setPos(pos)

    # -- 布局 ----------------------------------------------------------------

    @property
    def type_key(self) -> str:
        return f"{self.plugin}/{self.action}"

    @property
    def title(self) -> str:
        return self.custom_title or str(self.spec.get("name") or self.action)

    @property
    def category(self) -> str:
        return str(self.spec.get("category") or "通用")

    @property
    def is_trigger(self) -> bool:
        return self.spec.get("kind") == "trigger"

    @property
    def is_data_only(self) -> bool:
        """这个节点是不是"只出数据"的（画布上那个「值」）。

        看**动作的声明**，不看它有没有连线 —— 后者会把一个刚拖进来、还没连线的
        普通节点也当数据节点，于是它的执行口会莫名其妙消失。
        """
        return bool(self.spec.get("data_only"))

    def branch_names(self) -> list[str]:
        """这个节点的分支出口名（多路分支）。没有就返回空列表。

        **共用内核那份规则**（``kernel.graph.branch_names``）。各写一份的话，两边对
        "去不去重、逗号算不算分隔符"的理解迟早分叉 —— 表现是画布上看得见一个出口、
        运行时却说没有这条分支。
        """
        return branch_names(self.spec, self.params)

    def _build_ports(self) -> None:
        # 重建前先清掉旧的。分支出口会随参数变，不清的话改一次 cases 就多出一堆
        # 叠在一起的旧端口 —— 画布上看不出来，但连线时会连到已经不存在的出口上。
        for port in [*self.input_ports.values(), *self.output_ports.values()]:
            port.setParentItem(None)
            if port.scene() is not None:
                port.scene().removeItem(port)
        self.input_ports.clear()
        self.output_ports.clear()
        self.prepareGeometryChange()

        inputs: Mapping[str, Mapping[str, Any]] = self.spec.get("inputs") or {}
        outputs: Mapping[str, Mapping[str, Any]] = self.spec.get("outputs") or {}

        left: list[tuple[str, str, str, str]] = []
        # 触发器和纯数据节点都没有执行入口：
        # - 触发器是流程的起点，本来就不该有入口
        # - 纯数据节点（画布上那个「值」）不参与先后顺序，画个三角只会让人以为
        #   "必须把它接进链条里"，然后去连一条根本不需要的执行线
        if not self.is_trigger and not self.is_data_only:
            left.append((EXEC_IN_PORT, KIND_EXEC, "any", ""))
        for name, schema in inputs.items():
            left.append((name, KIND_DATA, str(schema.get("kind", "any")), str(schema.get("label") or name)))

        right: list[tuple[str, str, str, str]] = []
        # 纯数据节点没有执行出口 —— 它只出数据。
        branches = self.branch_names() if not self.is_data_only else []
        self._branches = list(branches)
        if branches:
            # 声明了分支出口的节点：每条分支一个三角出口，再加一个「出错」。
            #
            # **没有 success。** 分支本身就是走通的那条路 —— 再留一个 success，用户
            # 不知道该连哪个，而且引擎永远不会返回它（它只会返回某条分支名）。
            right.extend((name, KIND_EXEC, "any", name) for name in branches)
            right.append((PORT_ERROR, KIND_EXEC, "any", "出错"))
        elif not self.is_data_only:
            right = [
                (PORT_SUCCESS, KIND_EXEC, "any", "成功"),
                (PORT_ERROR, KIND_EXEC, "any", "出错"),
            ]
        for name, schema in outputs.items():
            right.append((name, KIND_DATA, str(schema.get("kind", "any")), str(schema.get("label") or name)))

        rows = max(len(left), len(right), 1)
        self._height = (
            theme.NODE_HEADER_HEIGHT
            + theme.NODE_PADDING_TOP
            + rows * theme.NODE_ROW_HEIGHT
            + theme.NODE_PADDING_BOTTOM
        )

        for index, (name, kind, data_type, label) in enumerate(left):
            port = PortItem(self, name, DIR_IN, kind, data_type=data_type, label=label, parent=self)
            port.setPos(QPointF(0.0, self._row_y(index)))
            self.input_ports[name] = port

        for index, (name, kind, data_type, label) in enumerate(right):
            port = PortItem(self, name, DIR_OUT, kind, data_type=data_type, label=label, parent=self)
            port.setPos(QPointF(theme.NODE_WIDTH, self._row_y(index)))
            self.output_ports[name] = port

        self._left_rows = left
        self._right_rows = right

    def _row_y(self, index: int) -> float:
        return (
            theme.NODE_HEADER_HEIGHT
            + theme.NODE_PADDING_TOP
            + index * theme.NODE_ROW_HEIGHT
            + theme.NODE_ROW_HEIGHT / 2.0
        )

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, theme.NODE_WIDTH, self._height)

    def port(self, name: str, direction: str) -> PortItem | None:
        table = self.output_ports if direction == DIR_OUT else self.input_ports
        return table.get(name)

    def all_ports(self) -> list[PortItem]:
        return list(self.input_ports.values()) + list(self.output_ports.values())

    # -- 绘制 ----------------------------------------------------------------

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.boundingRect()
        radius = theme.NODE_RADIUS
        selected = self.isSelected()

        body_color = QColor(theme.NODE_DISABLED_BODY if self.disabled else theme.NODE_BODY)
        if selected:
            body_color = QColor(theme.NODE_BODY_SELECTED)

        # --- 运行态外发光 ---
        state_color = theme.STATE_COLORS.get(self.run_state)
        if state_color:
            glow = QColor(state_color)
            glow.setAlpha(48)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawRoundedRect(rect.adjusted(-5, -5, 5, 5), radius + 5, radius + 5)
        if selected:
            accent = QColor(theme.NODE_BORDER_SELECTED)
            accent.setAlpha(40)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), radius + 3, radius + 3)

        # --- 主体 ---
        body = QPainterPath()
        body.addRoundedRect(rect.adjusted(0.0, 0.0, -1.0, -1.0), radius, radius)
        painter.fillPath(body, body_color)

        border = QColor(state_color or theme.NODE_BORDER)
        if selected:
            border = QColor(theme.NODE_BORDER_SELECTED)
        if self.missing:
            border = QColor(theme.STATE_FAILED)
        painter.setPen(QPen(border, 2.2 if (selected or state_color) else 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(body)

        # --- 表头 ---
        header_height = theme.NODE_HEADER_HEIGHT
        accent_color = QColor(theme.category_color(self.category))
        if self.missing:
            accent_color = QColor(theme.STATE_FAILED)

        header_fill = QColor(accent_color)
        header_fill.setAlpha(52)
        painter.fillPath(_top_rounded_path(theme.NODE_WIDTH - 1.0, header_height, radius), header_fill)

        strip = QPainterPath()
        strip.addRoundedRect(QRectF(0.0, 0.0, 4.0, header_height), 2.0, 2.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent_color)
        painter.drawPath(strip)
        painter.setPen(QPen(QColor(theme.NODE_BORDER), 1.0))
        painter.drawLine(QPointF(0.0, header_height), QPointF(theme.NODE_WIDTH, header_height))

        # --- 表头文字 ---
        title_font = QFont()
        title_font.setPointSizeF(10.5)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(theme.NODE_TEXT) if not self.disabled else QColor(theme.NODE_SUBTITLE))
        metrics = QFontMetrics(title_font)
        badge_width = self._badge_width(metrics)
        title = metrics.elidedText(self.title, Qt.TextElideMode.ElideRight, int(theme.NODE_WIDTH - 26 - badge_width))
        painter.drawText(QPointF(14.0, 19.0), title)

        subtitle_font = QFont()
        subtitle_font.setPointSizeF(8.0)
        painter.setFont(subtitle_font)
        painter.setPen(QColor(theme.NODE_SUBTITLE))
        subtitle = "插件未安装" if self.missing else self.type_key
        painter.drawText(
            QPointF(14.0, 35.0),
            QFontMetrics(subtitle_font).elidedText(subtitle, Qt.TextElideMode.ElideMiddle, int(theme.NODE_WIDTH - 26)),
        )

        self._paint_badge(painter, rect)

        # --- 端口标签 ---
        label_font = QFont()
        label_font.setPointSizeF(8.0)
        painter.setFont(label_font)
        label_metrics = QFontMetrics(label_font)
        for index, (name, kind, data_type, label) in enumerate(self._left_rows):
            if not label:
                continue
            color = QColor("#a8b2c0" if kind == KIND_EXEC else theme.NODE_TEXT)
            color.setAlpha(230)
            painter.setPen(color)
            painter.drawText(
                QPointF(13.0, self._row_y(index) + 3.5),
                label_metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(theme.NODE_WIDTH * 0.5)),
            )
        for index, (name, kind, data_type, label) in enumerate(self._right_rows):
            if not label:
                continue
            color = QColor("#a8b2c0" if kind == KIND_EXEC else theme.NODE_TEXT)
            color.setAlpha(230)
            painter.setPen(color)
            text = label_metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(theme.NODE_WIDTH * 0.45))
            width = label_metrics.horizontalAdvance(text)
            painter.drawText(QPointF(theme.NODE_WIDTH - 13.0 - width, self._row_y(index) + 3.5), text)

        if self.disabled:
            painter.setPen(QColor(theme.NODE_SUBTITLE))
            disabled_font = QFont()
            disabled_font.setPointSizeF(7.5)
            painter.setFont(disabled_font)
            painter.drawText(
                QPointF(14.0, self._height - 4.0),
                "已禁用，运行时会跳过",
            )

    def _badge_width(self, metrics: QFontMetrics) -> float:
        if self.run_state in ("success", "failed") and self.run_duration_ms is not None:
            return metrics.horizontalAdvance(f"{self.run_duration_ms / 1000:.2f}s") + 16
        if self.run_state == "running":
            return 30.0
        return 0.0

    def _paint_badge(self, painter: QPainter, rect: QRectF) -> None:
        if not self.run_state:
            return
        font = QFont()
        font.setPointSizeF(7.5)
        font.setBold(True)
        painter.setFont(font)
        metrics = QFontMetrics(font)

        if self.run_state == "running":
            text = "运行中"
        elif self.run_state == "skipped":
            text = "跳过"
        elif self.run_duration_ms is not None:
            text = f"{self.run_duration_ms / 1000:.2f}s"
        else:
            text = "完成" if self.run_state == "success" else "失败"

        width = metrics.horizontalAdvance(text) + 12.0
        badge = QRectF(theme.NODE_WIDTH - width - 10.0, 9.0, width, 17.0)
        color = QColor(theme.STATE_COLORS.get(self.run_state, theme.STATE_IDLE))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(badge, 7.0, 7.0)

        painter.setPen(QColor("#1b1f25"))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)

    # -- 状态 ----------------------------------------------------------------

    def refresh_ports(self) -> list[str] | None:
        """参数变了之后重建出口。

        返回 ``None`` 表示**什么都没变，没有重建**；返回列表表示重建过了，列表里是
        **被删掉**的出口名（可能是空的，那是"只加了新出口"）。

        这个区分是必要的：调用方只有在真的重建过之后才需要去重新指向那些边 ——
        而"重建过但没删东西"和"根本没重建"是两回事，边指向的对象在前者已经换了一批。

        调用方拿到被删掉的名字后要把挂在它们上面的连线一起删掉。**不删的话会留下
        "连着一条不存在的出口"的边** —— 画布上看还连得好好的，运行时那条路永远走不到。
        """
        wanted = self.branch_names()
        if wanted == self._branches:
            return None
        removed = [name for name in self._branches if name not in wanted]
        self._build_ports()
        return removed

    def set_run_state(self, state: str, *, duration_ms: int | None = None, message: str | None = None) -> None:
        self.run_state = state
        self.run_duration_ms = duration_ms
        self.run_message = message
        self.setToolTip(f"{self.title}\n{self.type_key}" + (f"\n{message}" if message else ""))
        self.update()

    def clear_run_state(self) -> None:
        self.set_run_state("")

    def itemChange(self, change: Any, value: Any) -> Any:
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            scene = self.scene()
            if scene is not None and hasattr(scene, "refresh_edges_for") and not getattr(scene, "_loading", False):
                scene.refresh_edges_for(self.node_id)
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.update()
        return super().itemChange(change, value)

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"<NodeItem {self.node_id} {self.type_key}>"
