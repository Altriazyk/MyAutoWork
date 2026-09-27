"""左侧导航栏。

只出现在首页（流程列表 / 插件）这两页；进编辑器后左边让给模块面板，它自动收起。

导航项（自动化流程 / 插件）和操作项（新建 / 打开）外观上分开，因为它们性质不同：
前者切页面，后者马上做一件事。混在一起用户会以为"新建"也是个页面。

**这里不放"展示方式"切换** —— 那是主区自己怎么排的问题，归顶部「视图」菜单管。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme

__all__ = [
    "SideBar",
    "SIDEBAR_WIDTH",
    "PAGE_WORKFLOWS",
    "PAGE_PLUGINS",
    "PAGE_CUSTOM",
    "PAGE_CHECKLIST",
]

PAGE_WORKFLOWS = "workflows"
PAGE_PLUGINS = "plugins"
PAGE_CUSTOM = "custom"
PAGE_CHECKLIST = "checklist"

#: 侧边栏固定宽度。放在这里导出，主窗口排分隔器时要用同一个数。
SIDEBAR_WIDTH = 190

_WIDTH = SIDEBAR_WIDTH


class SideBar(QWidget):
    pageRequested = Signal(str)
    newRequested = Signal()
    newModuleRequested = Signal()
    newChecklistRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sideBar")
        self.setFixedWidth(_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(1)

        # --- 导航 ---
        layout.addWidget(self._section("导航"))
        self.page_group = QButtonGroup(self)
        self.page_group.setExclusive(True)
        self.nav_workflows = self._nav_button("我的流程", PAGE_WORKFLOWS, checked=True)
        self.nav_plugins = self._nav_button("我的插件", PAGE_PLUGINS)
        self.nav_custom = self._nav_button("我的模块", PAGE_CUSTOM)
        self.nav_checklist = self._nav_button("我的清单", PAGE_CHECKLIST)
        for button in (self.nav_workflows, self.nav_plugins, self.nav_custom, self.nav_checklist):
            layout.addWidget(button)

        # --- 操作 ---
        layout.addWidget(self._section("操作"))

        # 这两个是"二级页面"的入口，都会亮。**必须互斥**，否则会出现「新建模块」和
        # 「新建清单」同时按下的样子 —— 用户根本看不出自己在哪个页面。
        # ``_action_button`` 不往任何组里加（导航项走的是 page_group），所以这里单独建一个。
        self.action_group = QButtonGroup(self)
        self.action_group.setExclusive(True)

        self.action_new = self._action_button("新建流程")
        self.action_new.clicked.connect(self.newRequested)
        layout.addWidget(self.action_new)

        self.action_new_module = self._action_button("新建模块")
        self.action_new_module.setToolTip("创建一个自己的模块，直接在右侧写代码")
        # 可选中：进模块编辑器时它要保持"按下"的样子 —— 那才是"我现在在这里"。
        # 编辑已有模块时也亮它，因为走的是同一个界面、同一件事（写模块）。
        self.action_new_module.setCheckable(True)
        self.action_new_module.clicked.connect(self.newModuleRequested)
        layout.addWidget(self.action_new_module)

        self.action_new_checklist = self._action_button("新建清单")
        self.action_new_checklist.setToolTip("建一份清单，然后往里面勾要执行的流程")
        # 和「新建模块」一样可勾选：进清单编辑器时要亮起来，否则用户在编辑器里
        # 看不出自己在哪一页、也不知道是怎么进来的。
        self.action_new_checklist.setCheckable(True)
        self.action_new_checklist.clicked.connect(self.newChecklistRequested)
        layout.addWidget(self.action_new_checklist)

        self.action_group.addButton(self.action_new_module)
        self.action_group.addButton(self.action_new_checklist)

        layout.addStretch(1)

        self.footer = QLabel()
        self.footer.setObjectName("sideBarFooter")
        self.footer.setWordWrap(True)
        layout.addWidget(self.footer)

        self.page_group.buttonClicked.connect(self._on_page_clicked)

    # -- 构建 ----------------------------------------------------------------

    def _section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sideBarSection")
        return label

    def _nav_button(
        self,
        text: str,
        value: str,
        *,
        checked: bool = False,
        group: QButtonGroup | None = None,
    ) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("sideBarButton")
        button.setCheckable(True)
        button.setChecked(checked)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setProperty("navValue", value)
        target_group = group if group is not None else self.page_group
        target_group.addButton(button)
        return button

    def _action_button(self, text: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("sideBarButton")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    # -- 交互 ----------------------------------------------------------------

    def _on_page_clicked(self, button: Any) -> None:
        value = button.property("navValue")
        if value:
            self.pageRequested.emit(str(value))

    # -- 状态 ----------------------------------------------------------------

    def set_page(self, page: str) -> None:
        buttons = {
            PAGE_WORKFLOWS: self.nav_workflows,
            PAGE_PLUGINS: self.nav_plugins,
            PAGE_CUSTOM: self.nav_custom,
            PAGE_CHECKLIST: self.nav_checklist,
        }
        button = buttons.get(page)
        if button is not None:
            button.setChecked(True)

    def clear_page_selection(self) -> None:
        """把所有导航项的选中态清掉。

        进模块编辑器时要调它：那里高亮的是操作区的「新建模块」，不是任何一个导航项。
        不清的话「插件」会和「新建模块」同时亮着，看起来像两个地方被打开了。

        互斥组里不能直接把已选中项 setChecked(False) —— 得先关掉互斥，否则它拒绝被
        取消选中。
        """
        checked = self.page_group.checkedButton()
        if checked is not None:
            self.page_group.setExclusive(False)
            checked.setChecked(False)
            self.page_group.setExclusive(True)

    def _uncheck(self, button: QPushButton) -> None:
        """把操作列里的按钮熄掉。

        **不能直接 ``setChecked(False)``。** 互斥组里那样是不生效的 —— Qt 认为"总得有一个
        是按下的"。`clear_page_selection` 当初就是踩了这个坑才写了同样的手法：先把互斥
        关掉，改完再打开。
        """
        if not button.isChecked():
            return
        self.action_group.setExclusive(False)
        button.setChecked(False)
        self.action_group.setExclusive(True)

    def set_module_action_active(self, active: bool) -> None:
        if active:
            self.action_new_module.setChecked(True)
        else:
            self._uncheck(self.action_new_module)

    def set_checklist_action_active(self, active: bool) -> None:
        """进/出清单编辑器时点亮或熄灭「新建清单」。

        和 ``set_module_action_active`` 是同一个道理：编辑器是二级页面，左侧没有任何导航项
        处于选中状态，不把来路那个按钮点亮的话，用户会觉得自己"掉进了一个不知道是什么的页面"。
        """
        if active:
            self.action_new_checklist.setChecked(True)
        else:
            self._uncheck(self.action_new_checklist)

    def set_enabled_actions(self, enabled: bool) -> None:
        self.action_new.setEnabled(enabled)
        self.action_new_module.setEnabled(enabled)
        self.action_new_checklist.setEnabled(enabled)

    def set_footer(self, text: str) -> None:
        self.footer.setText(text)
