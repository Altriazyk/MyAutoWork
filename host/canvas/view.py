"""画布视图：缩放、平移、框选、接受拖放。

交互约定尽量贴近大家已经用熟的节点编辑器：

- 滚轮缩放，以鼠标位置为中心
- 中键拖动平移
- 左键空白处拖动 = 框选
- Delete 删除选中
- Ctrl+0 复位缩放，F 适应窗口
"""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QFrame, QGraphicsView, QLabel

from .. import theme
from ..constants import NODE_MIME
from .scene import WorkflowScene


class CanvasView(QGraphicsView):
    """承载 WorkflowScene 的视图。"""

    zoomChanged = Signal(float)

    #: 提示条距视口底边的高度。
    TOAST_MARGIN = 18
    #: 提示条默认停留时长。
    TOAST_MSEC = 4000

    def __init__(self, scene: WorkflowScene, parent: Any = None) -> None:
        super().__init__(scene, parent)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        # 自定义背景网格 + 会移动的图元，局部刷新容易留下残影。
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontSavePainterState, True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)

        self._panning = False
        self._pan_origin = QPointF()

        self._toast: QLabel | None = None
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._hide_toast)

    # -- 画布内提示 -----------------------------------------------------------

    def show_toast(self, text: str, msec: int | None = None) -> None:
        """在画布底部浮一条短提示。

        连线成功/失败这类反馈属于画布本地，就该在画布上说。放到窗口最底下的状态栏上，
        用户拖动连线时根本不会往那儿看 —— 那正是最需要看到"为什么连不上"的时刻。
        """
        if self._toast is None:
            self._toast = QLabel(self.viewport())
            self._toast.setObjectName("canvasToast")
            self._toast.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._toast.setText(text)
        self._toast.adjustSize()
        self._position_toast()
        self._toast.show()
        self._toast.raise_()
        self._toast_timer.start(self.TOAST_MSEC if msec is None else msec)

    def hide_toast(self) -> None:
        self._toast_timer.stop()
        self._hide_toast()

    def _hide_toast(self) -> None:
        if self._toast is not None:
            self._toast.hide()

    def _position_toast(self) -> None:
        if self._toast is None:
            return
        viewport = self.viewport().rect()
        size = self._toast.sizeHint()
        x = viewport.center().x() - size.width() // 2
        y = viewport.bottom() - size.height() - self.TOAST_MARGIN
        self._toast.move(
            max(viewport.left() + 8, x),
            max(viewport.top() + 8, y),
        )

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        super().resizeEvent(event)
        self._position_toast()

    @property
    def toast_text(self) -> str:
        """当前提示内容（空串表示没在显示）。测试用。"""
        if self._toast is None or self._toast.isHidden():
            return ""
        return self._toast.text()

    # -- 背景 ----------------------------------------------------------------

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor(theme.BACKGROUND))

        grid = theme.GRID_SIZE
        # 缩得太小时网格会糊成一片，还不如不画。
        if self.transform().m11() < 0.45:
            return
        if rect.width() / grid > 400 or rect.height() / grid > 400:
            return

        minor: list[QLineF] = []
        major: list[QLineF] = []

        start_x = math.floor(rect.left() / grid) * grid
        x = start_x
        while x < rect.right():
            line = QLineF(x, rect.top(), x, rect.bottom())
            (major if round(x / grid) % 5 == 0 else minor).append(line)
            x += grid

        start_y = math.floor(rect.top() / grid) * grid
        y = start_y
        while y < rect.bottom():
            line = QLineF(rect.left(), y, rect.right(), y)
            (major if round(y / grid) % 5 == 0 else minor).append(line)
            y += grid

        # 宽度 0 = cosmetic 线，无论怎么缩放都保持 1 像素，不会随放大变粗。
        if minor:
            painter.setPen(QPen(QColor(theme.GRID_LINE), 0))
            painter.drawLines(minor)
        if major:
            painter.setPen(QPen(QColor(theme.GRID_LINE_MAJOR), 0))
            painter.drawLines(major)

    # -- 缩放 ----------------------------------------------------------------

    def zoom_by(self, factor: float) -> None:
        current = self.transform().m11()
        target = current * factor
        if target < theme.MIN_ZOOM or target > theme.MAX_ZOOM:
            return
        self.scale(factor, factor)
        self.zoomChanged.emit(self.transform().m11())

    def reset_zoom(self) -> None:
        self.resetTransform()
        self.zoomChanged.emit(self.transform().m11())

    def current_zoom(self) -> float:
        return self.transform().m11()

    def fit_content(self) -> None:
        bounds = self.scene().itemsBoundingRect()
        if bounds.isNull() or bounds.width() < 1.0:
            self.reset_zoom()
            return
        self.fitInView(
            bounds.adjusted(-60.0, -60.0, 60.0, 60.0),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        self._clamp_zoom()
        self.zoomChanged.emit(self.transform().m11())

    def _clamp_zoom(self) -> None:
        scale = self.transform().m11()
        if scale < theme.MIN_ZOOM:
            factor = theme.MIN_ZOOM / scale
        elif scale > theme.MAX_ZOOM:
            factor = theme.MAX_ZOOM / scale
        else:
            return
        self.scale(factor, factor)

    def center_on_node(self, node_id: str) -> None:
        item = self.scene().node_item(node_id) if isinstance(self.scene(), WorkflowScene) else None
        if item is not None:
            self.centerOn(item)

    # -- 鼠标 ----------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        self.zoom_by(1.15 if delta > 0 else 1.0 / 1.15)
        event.accept()

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_origin = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._panning:
            delta = event.position() - self._pan_origin
            self._pan_origin = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- 键盘 ----------------------------------------------------------------

    def keyPressEvent(self, event: Any) -> None:
        key = event.key()
        modifiers = event.modifiers()

        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            scene = self.scene()
            if isinstance(scene, WorkflowScene):
                scene.remove_selected()
                event.accept()
                return
        if key == Qt.Key.Key_0 and modifiers & Qt.KeyboardModifier.ControlModifier:
            self.reset_zoom()
            event.accept()
            return
        if key == Qt.Key.Key_F:
            self.fit_content()
            event.accept()
            return
        super().keyPressEvent(event)

    # -- 拖放 ----------------------------------------------------------------

    def dragEnterEvent(self, event: Any) -> None:
        if event.mimeData().hasFormat(NODE_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: Any) -> None:
        if event.mimeData().hasFormat(NODE_MIME):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: Any) -> None:
        if not event.mimeData().hasFormat(NODE_MIME):
            super().dropEvent(event)
            return

        payload = bytes(event.mimeData().data(NODE_MIME)).decode("utf-8")
        plugin, _, action = payload.partition("\n")
        scene = self.scene()
        if not isinstance(scene, WorkflowScene) or not plugin or not action:
            event.ignore()
            return

        scene_pos = self.mapToScene(event.position().toPoint())
        # 落到鼠标下方偏上一点，视觉上像是"抓在手里放下去"。
        item = scene.add_node(
            plugin,
            action,
            QPointF(scene_pos.x() - theme.NODE_WIDTH / 2.0, scene_pos.y() - 20.0),
        )
        scene.clearSelection()
        item.setSelected(True)
        event.acceptProposedAction()
