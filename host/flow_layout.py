"""流式布局：从左到右摆，摆不下就换行。

Qt 没有内置这种布局（``QGridLayout`` 要求你事先知道列数，``QHBoxLayout`` 不会换行）。
但"卡片墙"必须随窗口宽度自动决定每行放几张 —— 窗口拉宽就多放一张，拉窄就少放一张，
所以只能自己实现。

实现要点：

- ``hasHeightForWidth()`` 返回 True，让父级滚动区能按宽度反推高度
- ``heightForWidth()`` 和 ``setGeometry()`` 共用同一段排版逻辑（``_do_layout``），
  只是前者不真的摆 —— 两边算法不一致的话，滚动条长度会算错
"""

from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QSizePolicy, QWidget

__all__ = ["FlowLayout"]


class FlowLayout(QLayout):
    def __init__(
        self,
        parent: QWidget | None = None,
        margin: int = 0,
        h_spacing: int = 12,
        v_spacing: int = 12,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self.setContentsMargins(QMargins(margin, margin, margin, margin))

    # -- QLayout 必须实现的几个 -------------------------------------------------

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt 重写
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt 重写
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt 重写
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 - Qt 重写
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt 重写
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt 重写
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt 重写
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 重写
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt 重写
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )

    # -- 排版 -----------------------------------------------------------------

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue

            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_spacing
            # 这一行放不下了就换行。line_height > 0 保证至少每行放一个，
            # 否则窄窗口里的宽卡片会被挤成无限循环。
            if next_x - self._h_spacing > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + self._v_spacing
                next_x = x + hint.width() + self._h_spacing
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))

            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()


def flow_height_hint(layout: FlowLayout, width: int) -> int:
    """给滚动区用：算一下这个宽度下需要多高。"""
    return layout.heightForWidth(width)


def make_expanding(widget: QWidget) -> None:
    widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
