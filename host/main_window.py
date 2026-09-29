"""主窗口。

窗口结构（从上到下）：

    ┌──────────────────────────────────────────────────────┐
    │ 文件 编辑 视图 运行 帮助            ─  □  ✕          │  ← 自绘标题栏（无边框窗口）
    ├──────────┬───────────────────────────────────────────┤
    │ 导航      │                                           │
    │  自动化流程│           页面内容                        │
    │  插件     │      （流程列表 / 插件 / 画布）            │
    │ 操作      │                                           │
    │ 展示方式  │                                           │
    ├──────────┴───────────────────────────────────────────┤
    │ 运行日志 / 变量 / 运行历史                             │
    └──────────────────────────────────────────────────────┘

**没有系统标题栏**：那一行本来只写着一个软件名，不如让给菜单。代价是拖动和边缘缩放要
自己接管（见 ``titlebar.py`` 和下面的 ``nativeEvent``）。软件名暂时不显示。

三个页面用 ``QStackedWidget`` 切换。侧边栏只在"流程列表 / 插件"这两页出现；进编辑器后
左边让给模块面板，它自动收起。

一条硬规则：**不在运行期弹模态对话框。** 自动化可能跑几分钟，日志是唯一的现场记录。
用模态框报"运行失败"，用户第一反应是点掉它 —— 点掉之后才想起还没看日志。所以运行结果、
校验错误、拾取器未实现这类信息一律进日志面板和状态栏。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, QPointF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from kernel.checklist import (
    ChecklistSettings,
    load as load_checklist_file,
    resolve_path as resolve_checklist_path,
)
from kernel.errors import MyAutoWorkError
from kernel.install import (
    PluginPackageError,
    inspect as inspect_package,
    install as install_package,
    plan as plan_package,
)
from kernel.graph import Node, Workflow, load_workflow, save_workflow
from kernel.library import WorkflowEntry, WorkflowLibrary

#: 开机启动项的引导文件要写死这个仓库根路径 —— 开机时没有"当前目录"这回事。
REPO_ROOT = Path(__file__).resolve().parent.parent

from . import theme
from .ai_panel import AiMessage, AiPanel, StubBackend
from .ai_backend import ChatBackend
from .ai_settings import (
    AiSettings,
    load_settings as load_ai_settings,
    resolve_api_key,
    save_settings as save_ai_settings,
)
from .ai_settings_dialog import SettingsDialog
from .canvas import CanvasView, WorkflowScene
from .constants import VIEW_CARD, VIEW_LIST
from .panels import InspectorPanel, LogPanel, PalettePanel
from .custom_view import CustomModuleView, workflows_using
from . import startup, theme
from .checklist_view import (
    CHECKLIST_FILE,
    DEFAULT_CHECKLIST,
    ChecklistEditorView,
    ChecklistSettingsDialog,
    ChecklistView,
    load_checklists,
    save_checklists,
)
from .dep_installer import DependencyInstallThread, describe_target, target_python
from .module_editor import ModuleEditorView
from .plugin_view import PluginDetailsDialog, PluginListView
from .web_tool import WebToolWindow
from .runner import WorkflowRunThread
from .sidebar import (
    PAGE_CHECKLIST,
    PAGE_CUSTOM,
    PAGE_PLUGINS,
    PAGE_WORKFLOWS,
    SIDEBAR_WIDTH,
    SideBar,
)
from .uia_picker import ElementPicker
from .titlebar import TITLE_BAR_HEIGHT, TitleBar
from .workflow_list import WorkflowListView

APP_NAME = "myautowork"

MODE_WORKFLOWS = "workflows"
MODE_PLUGINS = "plugins"
MODE_CUSTOM = "custom"
MODE_CHECKLIST = "checklist"
MODE_CHECKLIST_EDIT = "checklist_edit"
MODE_MODULE = "module"
MODE_EDITOR = "editor"

#: 底边栏（运行日志 / 变量 / 运行历史 / AI 对话）的初始高度。
#: 400 试过，太占地方 —— 主区被压得只剩一条。200 够看几行日志。
LOG_PANEL_HEIGHT = 200

# --- Windows 原生命中测试：把窗口边缘映射成原生缩放热区 --------------------- #
_NATIVE_RESIZE = sys.platform == "win32"
_WM_NCHITTEST = 0x0084
_HTLEFT, _HTRIGHT, _HTTOP, _HTTOPLEFT, _HTTOPRIGHT = 10, 11, 12, 13, 14
_HTBOTTOM, _HTBOTTOMLEFT, _HTBOTTOMRIGHT = 15, 16, 17
_RESIZE_MARGIN = 6

#: 补窗口样式时用的几个 Win32 常量。
_GWL_STYLE = -16
_WS_THICKFRAME = 0x00040000
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOZORDER, _SWP_NOACTIVATE, _SWP_FRAMECHANGED = (
    0x0001,
    0x0002,
    0x0004,
    0x0010,
    0x0020,
)

#: Windows 11 的 DWM 圆角。属性号 33 是 ``DWMWA_WINDOW_CORNER_PREFERENCE``，
#: 值 2 是 ``DWMWCP_ROUND``。Windows 10 上这个调用会失败（E_INVALIDARG），据此降级。
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUND = 2

#: 窗口圆角半径（逻辑像素）。比按钮的 6 大一点 —— 窗口的角看起来比控件的角"重"。
_WINDOW_RADIUS = 10


def _signed16(value: int) -> int:
    """Windows 把屏幕坐标的两个 16 位分量打包进 lParam，负数要自己还原。"""
    return value - 0x10000 if value & 0x8000 else value


class MainWindow(QMainWindow):
    #: 插件进程的 stderr 从 RPC 读取线程转发过来。必须走信号，不能直接碰控件 ——
    #: Qt 控件只能在主线程操作，从别的线程调用是随机崩溃。
    workerStderr = Signal(str)

    def __init__(
        self,
        registry: Any,
        *,
        store: Any = None,
        workdir: Path | None = None,
        workflows_dir: Path | None = None,
        plugins_dir: Path | None = None,
        timeout: float = 300.0,
        ai_settings: Any = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        # 无边框：系统标题栏那一条不要了。
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        self.registry = registry
        self.store = store
        self.workdir = Path(workdir or Path.cwd()).resolve()
        self.workflows_dir = Path(workflows_dir) if workflows_dir else self.workdir / "workflows"
        # AI 助手产出的模块要落到真实的插件目录里，所以这里得知道它在哪。
        self.plugins_dir = (
            Path(plugins_dir)
            if plugins_dir
            else Path(getattr(registry, "plugins_dir", self.workdir / "plugins"))
        )
        self.timeout = timeout
        # **AI 设置可以从外面传进来。** 测试必须能传一份"没配过"的设置 —— 否则测试结果会
        # 取决于跑测试那台机器的 %APPDATA% 里有没有 ai.json。测试不该依赖机器上装了什么。
        self.ai_settings = ai_settings if ai_settings is not None else load_ai_settings()
        self.library = WorkflowLibrary(self.workflows_dir, store=store)

        self.current_path: Path | None = None
        self.confirm_on_close = True
        self._mode = MODE_WORKFLOWS
        self._dirty = False
        self._loading_document = False
        self._run_thread: WorkflowRunThread | None = None
        self._run_from_editor = False
        self._run_entry_path: Path | None = None
        #: 原生命中测试是否还可用。失败一次就永久关掉，别在每次鼠标移动上重试。
        self._native_resize_ok = True
        #: 补 WS_THICKFRAME 只做一次（showEvent 里做）。
        self._thickframe_done = False
        #: 网页工具窗口。**只建一次然后复用** —— 每次点菜单都新建的话，
        #: 旧窗口的轮询定时器还在跑，会重复问插件要数据。
        self._web_tool: WebToolWindow | None = None
        #: 圆角也只做一次；``_region_corners`` 为 True 表示走的是自己裁那条退路。
        self._corners_done = False
        self._region_corners = False
        #: 清单执行队列（按顺序跑）与进度。
        self._checklist_queue: list[Path] = []
        self._checklist_index = -1
        self._checklist_failed: list[str] = []
        self._checklist_running = ""
        #: 每条清单自己的设置（现在只有开机运行）。**跟 _checklists 分开存** ——
        #: 一个是"内容"，一个是"配置"，混在一起以后加设置项就要动一堆地方。
        self._checklist_settings_map: dict[str, ChecklistSettings] = {}
        #: 正在跑的依赖安装线程（同时只允许一个）。
        self._install_thread: Any = None
        self._last_run_status = ""
        #: 左侧栏是不是被用户收起来了（编辑器里它是自动收起的，那是另一回事）。
        self._sidebar_visible = True
        #: 所有清单：{名字: [流程文件名]}，以及当前选中的那条。
        self._checklists: dict[str, list[str]] = {}
        self._current_checklist = ""

        self._build_shell()
        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_panels()
        self._connect()

        self.resize(1480, 920)
        self.workflow_list.reload()
        self._refresh_footer()
        self._show_page(PAGE_WORKFLOWS)
        self.log_panel.refresh_history()

    # -- 外壳 ----------------------------------------------------------------

    def _build_shell(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self._root_layout = root_layout

        # 菜单挂在自绘标题栏里，而不是 QMainWindow.menuBar()。
        self.menu_bar = QMenuBar()
        self.title_bar = TitleBar(self.menu_bar, self)
        root_layout.addWidget(self.title_bar)
        # 工具栏在下面插入（索引 1）。不能用 addToolBar()：QMainWindow 会把它排到
        # 中央控件**上面**，也就是压到自绘标题栏头顶上。

        # --- 三个页面 ---
        self.workflow_list = WorkflowListView(self.library, self)
        self.plugin_view = PluginListView(self.registry, self)
        self.custom_view = CustomModuleView(self.registry, self.workflows_dir, self)
        self.scene = WorkflowScene(self.registry, self)
        self.scene.workflow_id = "untitled"
        self.scene.workflow_name = "未命名工作流"
        self.view = CanvasView(self.scene, self)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.workflow_list)  # 0
        self.stack.addWidget(self.plugin_view)  # 1
        self.stack.addWidget(self.view)  # 2
        self.stack.addWidget(self.custom_view)  # 3
        self.module_editor = ModuleEditorView(self.registry, self)
        self.stack.addWidget(self.module_editor)  # 4
        self.checklist_view = ChecklistView(self)
        self.stack.addWidget(self.checklist_view)  # 5
        self.checklist_editor = ChecklistEditorView(self)
        self.stack.addWidget(self.checklist_editor)  # 6
        # 显式给一个下限，否则表格页会把窗口的最小宽度顶得很大、根本拉不窄。
        self.stack.setMinimumWidth(320)

        self.sidebar = SideBar(self)

        # 面板区由 _build_panels 用分隔器填充。
        self.body = QWidget()
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        root_layout.addWidget(self.body, 1)

        self.setCentralWidget(root)

    def _build_actions(self) -> None:
        self.action_home = QAction("← 流程列表", self)
        self.action_home.setShortcut("Ctrl+H")
        # **必须走 back_to_list()，不能直接 _show_page。** 直接切页就绕过了"要不要保存"
        # 那一问 —— 而返回之后再点「编辑」是从磁盘重读，手上的改动就没了。之前这里写的
        # 是 lambda: _show_page(...)，于是 back_to_list 成了一个没人调用的死方法。
        self.action_home.triggered.connect(self.back_to_list)

        self.action_new = QAction("新建", self)
        self.action_new.setShortcut("Ctrl+N")
        self.action_new.triggered.connect(self.new_workflow)

        self.action_open = QAction("打开…", self)
        self.action_open.setShortcut("Ctrl+O")
        self.action_open.triggered.connect(self.open_workflow)

        self.action_save = QAction("保存", self)
        self.action_save.setShortcut("Ctrl+S")
        self.action_save.triggered.connect(self.save_workflow)

        self.action_save_as = QAction("另存为…", self)
        self.action_save_as.setShortcut("Ctrl+Shift+S")
        self.action_save_as.triggered.connect(self.save_workflow_as)

        self.action_quit = QAction("退出", self)
        self.action_quit.setShortcut("Ctrl+Q")
        self.action_quit.triggered.connect(self.close)

        self.action_delete = QAction("删除选中", self)
        self.action_delete.setShortcut("Del")
        self.action_delete.triggered.connect(self.scene.remove_selected)

        self.action_run = QAction("运行", self)
        self.action_run.setShortcut("F5")
        self.action_run.triggered.connect(self.run_current_workflow)

        self.action_stop = QAction("停止", self)
        self.action_stop.setShortcut("Shift+F5")
        self.action_stop.setEnabled(False)
        self.action_stop.triggered.connect(self.stop_workflow)

        self.action_zoom_in = QAction("放大", self)
        self.action_zoom_in.setShortcut("Ctrl+=")
        self.action_zoom_in.triggered.connect(lambda: self.view.zoom_by(1.2))

        self.action_zoom_out = QAction("缩小", self)
        self.action_zoom_out.setShortcut("Ctrl+-")
        self.action_zoom_out.triggered.connect(lambda: self.view.zoom_by(1 / 1.2))

        self.action_zoom_reset = QAction("100%", self)
        self.action_zoom_reset.setShortcut("Ctrl+0")
        self.action_zoom_reset.setToolTip("复位到 100%（按钮上显示的是当前缩放比例）")
        self.action_zoom_reset.triggered.connect(self.view.reset_zoom)

        self.action_fit = QAction("适应窗口", self)
        self.action_fit.setShortcut("Ctrl+Shift+F")
        self.action_fit.triggered.connect(self.view.fit_content)

        self.action_refresh = QAction("刷新", self)
        self.action_refresh.setShortcut("Ctrl+R")
        self.action_refresh.setToolTip("重新扫描流程目录和插件目录")
        self.action_refresh.triggered.connect(self._refresh_all)

        self.action_card_view = QAction("卡片视图（网格）", self)
        self.action_card_view.setShortcut("Ctrl+1")
        self.action_card_view.setCheckable(True)
        self.action_card_view.setChecked(True)
        self.action_card_view.triggered.connect(lambda: self._set_view_mode(VIEW_CARD))
        self.addAction(self.action_card_view)  # 菜单里不再列，但快捷键要留着

        self.action_list_view = QAction("列表视图（通栏）", self)
        self.action_list_view.setShortcut("Ctrl+2")
        self.action_list_view.setCheckable(True)
        self.action_list_view.triggered.connect(lambda: self._set_view_mode(VIEW_LIST))
        self.addAction(self.action_list_view)

        self.action_show_log = QAction("运行面板", self)
        self.action_show_log.setCheckable(True)
        self.action_show_log.setChecked(True)
        self.action_show_log.toggled.connect(self._toggle_log_panel)

        self.action_about = QAction("关于", self)
        self.action_about.triggered.connect(self._show_about)

    def _build_menus(self) -> None:
        """顶部菜单只放**整个软件**级别的功能。

        **编辑器专有的东西不在这里。** 保存 / 另存为 / 返回列表 / 删除选中 / 缩放 /
        运行当前流程 —— 这些只在画布打开时才有意义，而且工具栏上已经有按钮了。放进菜单
        等于同一件事有两个入口，还得在非编辑器状态下把它们藏起来（就是 `_apply_mode` 里
        那一串 setVisible）—— 藏来藏去本身就是"它们不该在这儿"的信号。

        从菜单去掉不影响它们：action 仍然挂在工具栏上，快捷键照常生效
        （Ctrl+S、Ctrl+0…… 由 action 自身承载，跟它出现在哪个菜单无关）。
        """
        file_menu = self.menu_bar.addMenu("文件")
        file_menu.addAction(self.action_new)
        file_menu.addAction(self.action_open)
        file_menu.addSeparator()
        file_menu.addAction(self.action_quit)

        edit_menu = self.menu_bar.addMenu("编辑")
        # 设置放这儿：它是"配置"，不是"功能" —— 左侧导航留给日常操作的页面。
        # 一个入口，左选类别右填内容；以后加设置只是加一页，不再多一个菜单项。
        self.action_ai_settings = QAction("设置…", self)
        self.action_ai_settings.setShortcut("Ctrl+,")
        self.action_ai_settings.setToolTip("接口、密钥、模型，以及告诉模型 SDK 长什么样的系统提示词")
        self.action_ai_settings.triggered.connect(self._open_ai_settings)
        edit_menu.addAction(self.action_ai_settings)

        self.view_menu = self.menu_bar.addMenu("视图")
        # 「卡片 / 列表」不在这里 —— 每个页面右上角都有自己的切换按钮。"这一页怎么排"
        # 是页面自己的事；放全局菜单里意味着切个页还要记得当前是哪种排法。
        self.action_theme = QAction("浅色主题", self)
        self.action_theme.setCheckable(True)
        self.action_theme.setShortcut("Ctrl+T")
        self.action_theme.setToolTip("在深色和浅色之间切换")
        self.action_theme.toggled.connect(self._set_theme)
        self.view_menu.addAction(self.action_theme)
        self.view_menu.addSeparator()
        self.view_menu.addAction(self.action_show_log)

        run_menu = self.menu_bar.addMenu("运行")
        run_menu.addAction(self.action_refresh)
        run_menu.addSeparator()
        # 只留「停止」：在首页点某张卡片的运行之后，总得有个地方能把它停下来。
        # 「运行当前流程」是编辑器的事，工具栏上有。
        run_menu.addAction(self.action_stop)

        # 插件页从左侧导航栏进，这里不再另开一个入口。
        tool_menu = self.menu_bar.addMenu("工具")
        self.action_web_tool = QAction("网页工具…", self)
        self.action_web_tool.setToolTip("实时显示你在网页上点了什么，并生成选择器")
        self.action_web_tool.triggered.connect(self._open_web_tool)
        tool_menu.addAction(self.action_web_tool)

        help_menu = self.menu_bar.addMenu("帮助")
        help_menu.addAction(self.action_about)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("主工具栏", self)
        toolbar.setObjectName("mainToolBar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        for action in (
            self.action_home,
            None,
            self.action_new,
            self.action_open,
            self.action_save,
            # 「另存为」也要在这儿 —— 它原来只在文件菜单里，菜单收紧之后如果工具栏也没它，
            # 这个功能就只剩快捷键，等于藏起来了。
            self.action_save_as,
            None,
            self.action_run,
            self.action_stop,
            None,
            self.action_zoom_out,
            self.action_zoom_reset,
            self.action_zoom_in,
            self.action_fit,
            None,
            self.action_delete,
        ):
            if action is None:
                toolbar.addSeparator()
            else:
                toolbar.addAction(action)
        self._root_layout.insertWidget(1, toolbar)
        self.toolbar = toolbar

    def _build_panels(self) -> None:
        """用分隔器排面板，而不是 QDockWidget。

        QDockWidget 占的是 QMainWindow 的**顶层**区域，于是自绘标题栏只能挤在中间：
        菜单被左右两个面板夹着，窗口按钮压在右侧面板上，连拖窗口都只有中间那一条能拖。
        自己用 QSplitter 排就没这个问题，顺便还能拖动调整宽度。
        """
        self.palette_panel = PalettePanel(self.registry, self)
        self.palette_panel.setMinimumWidth(210)

        self.inspector = InspectorPanel(self.scene, self)
        self.inspector.setMinimumWidth(260)

        self.log_panel = LogPanel(self.store, self)
        self.log_panel.setMinimumHeight(140)

        # AI 对话和运行日志并排放在底部面板：它们回答的是同一类问题 ——
        # "现在发生了什么，接下来该做什么"。
        self.ai_panel = AiPanel(self)
        self.log_panel.addTab(self.ai_panel, "AI 对话")
        self._apply_ai_backend()

        self.top_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.top_splitter.addWidget(self.sidebar)
        self.top_splitter.addWidget(self.palette_panel)
        self.top_splitter.addWidget(self.stack)
        self.top_splitter.addWidget(self.inspector)
        self.top_splitter.setStretchFactor(0, 0)
        self.top_splitter.setStretchFactor(1, 0)
        self.top_splitter.setStretchFactor(2, 1)
        self.top_splitter.setStretchFactor(3, 0)
        self.top_splitter.setCollapsible(2, False)  # 中间那页不能被拖没
        # 面板不能因为窗口被拉窄就自己折叠 —— 那样用户拖窄一次再拉回来，侧边栏和日志
        # 就永久消失了，还得手动去菜单里找回来。
        self.top_splitter.setChildrenCollapsible(False)
        self.top_splitter.setSizes([SIDEBAR_WIDTH, 0, 900, 0])

        self.main_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.main_splitter.addWidget(self.top_splitter)
        self.main_splitter.addWidget(self.log_panel)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 0)
        self.main_splitter.setCollapsible(0, False)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setSizes([max(300, 900 - LOG_PANEL_HEIGHT), LOG_PANEL_HEIGHT])
        # 记下底边栏当前高度：切页会重排面板，不能让它把用户拖出来的高度弹回去。
        self._log_height = LOG_PANEL_HEIGHT
        self.main_splitter.splitterMoved.connect(self._on_splitter_moved)

        self._body_layout.addWidget(self.main_splitter)

    def _notify(self, message: str, level: str = "info") -> None:
        """把一句反馈写进运行日志。

        以前这些走的是窗口最底下的状态栏。那条常驻的横条占着一行高度、没事的时候一片
        空白；而且用户在画布或列表上操作时根本不会往下看。日志面板本来就在视野里，
        还顺手留了痕。

        真正属于"局部"的反馈（画布连线）走 ``CanvasView.show_toast()``。
        """
        self.log_panel.append_log(level, message)

    def _connect(self) -> None:
        self.scene.statusMessage.connect(self.view.show_toast)
        self.scene.nodeSelected.connect(self.inspector.show_node)
        self.scene.workflowChanged.connect(self._on_scene_changed)
        self.view.zoomChanged.connect(self._on_zoom_changed)

        self.palette_panel.nodeActivated.connect(self.add_node_at_center)
        self.inspector.paramChanged.connect(self._mark_dirty)
        self.inspector.pickRequested.connect(self._on_pick_requested)
        # 页面右上角的切换按钮和「视图」菜单指向同一个状态，两边都要同步。
        self.workflow_list.toggle.changed.connect(self._set_view_mode)
        self.custom_view.deleteRequested.connect(self._delete_plugin)
        self.plugin_view.deleteRequested.connect(self._delete_plugin)
        self.plugin_view.revealRequested.connect(self._reveal_plugin)
        self.plugin_view.detailsRequested.connect(self._show_plugin_details)
        self.plugin_view.installRequested.connect(self._install_plugin_package)
        self.workflow_list.deleteRequested.connect(self._delete_workflow)
        self.custom_view.editRequested.connect(self._edit_module)
        self.custom_view.renameRequested.connect(self._rename_module)
        self.custom_view.revealRequested.connect(self._reveal_path)
        self.custom_view.openFolderRequested.connect(self._reveal_plugins_dir)
        # 只有「我的模块」页有安装入口 —— 插件页是只读的展示。
        self.custom_view.installRequested.connect(self._install_dependencies)

        self.checklist_view.newRequested.connect(self._begin_new_checklist)
        self.checklist_view.runRequested.connect(self._run_checklist)
        self.checklist_view.editRequested.connect(self._edit_checklist)
        self.checklist_view.renameRequested.connect(self._rename_checklist)
        self.checklist_view.deleteRequested.connect(self._delete_checklist)
        self.checklist_view.settingsRequested.connect(self._checklist_settings)
        self.checklist_view.stopRequested.connect(self.stop_workflow)

        self.checklist_editor.saved.connect(self._on_checklist_saved)
        self.checklist_editor.cancelled.connect(lambda: self._show_page(PAGE_CHECKLIST))
        self.title_bar.sidebarToggleRequested.connect(self._toggle_sidebar)

        self.module_editor.saved.connect(self._on_module_saved)
        self.module_editor.failed.connect(self._on_module_save_failed)
        self.module_editor.cancelled.connect(lambda: self._show_page(PAGE_CUSTOM))

        # 拾取器：把屏幕上的控件变成参数值。元素截图存在工作目录下的 screens/。
        self._pick_port = ""
        self.picker = ElementPicker(self, template_dir=self.workdir / "screens")
        self.picker.picked.connect(self._on_element_picked)
        self.picker.cancelled.connect(self._on_pick_cancelled)
        self.workerStderr.connect(self._on_worker_stderr)

        self.title_bar.minimizeRequested.connect(self.showMinimized)
        self.title_bar.maximizeRequested.connect(self._toggle_maximize)
        self.title_bar.closeRequested.connect(self.close)

        self.sidebar.pageRequested.connect(self._show_page)
        self.sidebar.newRequested.connect(self.new_workflow)
        self.sidebar.newModuleRequested.connect(self._new_module)
        self.sidebar.newChecklistRequested.connect(self._begin_new_checklist)

        self.ai_panel.draftSaveRequested.connect(self._save_plugin_draft)

        self.workflow_list.runRequested.connect(self.run_entry)
        # 「运行」按钮在跑的时候会就地变成「停止」，点它走这里。
        self.workflow_list.stopRequested.connect(self.stop_workflow)
        self.workflow_list.editRequested.connect(self.edit_entry)
        self.workflow_list.revealRequested.connect(self.reveal_entry)

    # -- 无边框窗口的拖动与缩放 -----------------------------------------------

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        if event.type() == event.Type.WindowStateChange:
            self.title_bar.set_maximized(self.isMaximized())
        super().changeEvent(event)

    def _native_to_local(self, x: int, y: int, ratio: float | None = None) -> QPoint:
        """把 ``WM_NCHITTEST`` 给的**物理像素**坐标换算成 Qt 的**逻辑坐标**。

        这两者不是一回事：``lParam`` 永远是物理像素，而 Qt 在 DPI 感知下的
        ``mapFromGlobal`` 要的是逻辑（设备无关）坐标。125% 缩放下差 1.25 倍 —— 不换算的话
        "窗口边缘 6 像素"这个判定带会被整体推出去一大截，后果是：

        - 边框拖不动（命中的位置和真实的边不重合）
        - **右上角的最大化按钮点不动** —— 它正好落在那片被误判成"缩放热区"的范围里，
          点击被系统当成拖边框吃掉了

        抽成一个函数是为了能单独测：离屏测试里窗口的 DPR 恒为 1，跑不出真实缩放。
        """
        scale = self.devicePixelRatioF() if ratio is None else ratio
        if scale <= 0:
            scale = 1.0
        return self.mapFromGlobal(QPoint(round(x / scale), round(y / scale)))

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        super().showEvent(event)
        # 只做一次。窗口还没有原生句柄的时候改样式是无效的，所以放在 show 之后。
        if not self._thickframe_done:
            self._thickframe_done = True
            self._enable_native_resize()
        if not self._corners_done:
            self._corners_done = True
            self._round_corners()

    def _round_corners(self) -> None:
        """给无边框窗口加圆角。优先走系统原生的那条路。

        - **Windows 11**：``DwmSetWindowAttribute(DWMWA_WINDOW_CORNER_PREFERENCE)``。
          这是正解 —— 系统自己裁，边缘**抗锯齿**，阴影也跟着一起处理。
        - **Windows 10**：没有这个 API（那次调用会返回 E_INVALIDARG），退回
          ``SetWindowRgn`` 自己裁一个圆角矩形。代价说清楚：**边缘有锯齿**，
          而且那个区域是窗口坐标系的，**每次改大小都得重设**。
        """
        if not _NATIVE_RESIZE:
            return
        try:
            import ctypes

            preference = ctypes.c_int(_DWMWCP_ROUND)
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()),
                _DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(preference),
                ctypes.sizeof(preference),
            )
            if result == 0:
                return  # 系统接管了，不用自己裁
        except Exception:  # pragma: no cover - 平台细节
            pass
        self._region_corners = True
        self._apply_region_corners()

    def _apply_region_corners(self) -> None:
        """``SetWindowRgn`` 那条退路：自己裁一个圆角矩形。"""
        try:
            import ctypes

            hwnd = int(self.winId())
            ratio = self.devicePixelRatioF() or 1.0
            # 区域用的是**物理像素**，所以要乘 DPR。
            width = int(self.width() * ratio)
            height = int(self.height() * ratio)
            radius = max(1, int(_WINDOW_RADIUS * ratio))
            region = ctypes.windll.gdi32.CreateRoundRectRgn(
                0, 0, width + 1, height + 1, radius * 2, radius * 2
            )
            if not region:
                self._region_corners = False
                return
            # **SetWindowRgn 会接管这个 region**，成功之后不要自己 DeleteObject ——
            # 删了就是把窗口正在用的东西释放掉。
            ctypes.windll.user32.SetWindowRgn(hwnd, region, True)
        except Exception:  # pragma: no cover - 平台细节
            self._region_corners = False

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 重写
        super().resizeEvent(event)
        # 自己裁的那条路上，圆角区域是窗口坐标系的 —— 窗口一大，旧区域就不对了
        # （表现是圆角被"截"在原来的大小上，右下角变成方的）。
        if self._region_corners:
            self._apply_region_corners()

    def _enable_native_resize(self) -> None:
        """给无边框窗口补上 ``WS_THICKFRAME`` —— 没有它，Windows 不执行缩放。

        **这是诊断出来的，不是猜的。** ``Qt.FramelessWindowHint`` 建出来的窗口是
        ``WS_POPUP``，**不带** ``WS_THICKFRAME``。而 Windows 只在窗口有这个样式时才认
        ``nativeEvent`` 返回的 ``HTLEFT`` / ``HTRIGHT`` 这些命中结果 —— 所以那边算得再对
        也没用：真实会话里实测 ``WM_NCHITTEST`` 收到了 1266 次、HT 码也正确，
        拖动就是没反应，因为 ``GWL_STYLE = 0x960B0000`` 里没有 ``0x00040000``。

        ``SetWindowPos(..., SWP_FRAMECHANGED)`` 那一步不能省：光改样式位，Windows 不会
        重新计算非客户区，新样式要等下一次窗口尺寸变化才生效。
        """
        if not _NATIVE_RESIZE:
            return
        try:
            import ctypes

            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, _GWL_STYLE)
            if style & _WS_THICKFRAME:
                return  # 已经有了（某些平台上 Qt 会自己加）
            user32.SetWindowLongW(hwnd, _GWL_STYLE, style | _WS_THICKFRAME)
            user32.SetWindowPos(
                hwnd,
                0,
                0,
                0,
                0,
                0,
                _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
            )
        except Exception:  # pragma: no cover - 平台细节
            # 补不上就退回到"不能拖边缘缩放"。不该因为这个让窗口打不开。
            self._native_resize_ok = False

    def nativeEvent(self, event_type: Any, message: Any) -> Any:  # noqa: N802 - Qt 重写
        """把窗口边缘 6 像素交给 Windows 的原生缩放逻辑。

        自己算鼠标偏移也能实现缩放，但拿不到原生的热区判定、光标形状、最大化时自动失效
        这些细节。返回 HTxxx 之后，剩下的都是系统的事。

        这个函数在**每一次鼠标移动**上都会被调用，所以判断顺序按"最便宜的先做"排：
        平台判断 -> 消息类型 -> 是否最大化 -> 才去碰 MSG 结构。任何一步出错就永久关掉
        这条路径，不要每次移动都重试一遍 —— 缩放不可用不该让窗口变卡。
        """
        if (
            _NATIVE_RESIZE
            and self._native_resize_ok
            and event_type == b"windows_generic_MSG"
            and not self.isMaximized()
        ):
            try:
                import ctypes.wintypes  # 只在 Windows 上有意义，延迟导入

                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == _WM_NCHITTEST:
                    packed = int(msg.lParam)
                    local = self._native_to_local(
                        _signed16(packed & 0xFFFF), _signed16((packed >> 16) & 0xFFFF)
                    )
                    # 标题栏上的按钮要能点到：那块地方属于窗口按钮，不该被当成缩放热区。
                    if local.y() <= TITLE_BAR_HEIGHT and self._over_title_button(local.x()):
                        return super().nativeEvent(event_type, message)
                    code = self._hit_test_edge(local)
                    if code is not None:
                        return True, code
            except Exception:  # pragma: no cover - 平台细节
                self._native_resize_ok = False
                return super().nativeEvent(event_type, message)
        return super().nativeEvent(event_type, message)

    def _over_title_button(self, local_x: int) -> bool:
        """这个横坐标是不是落在标题栏右边的窗口按钮上。"""
        bar = self.title_bar
        if bar is None:
            return False
        for button in (bar.sidebar_button, bar.min_button, bar.max_button, bar.close_button):
            left = button.mapTo(self, QPoint(0, 0)).x()
            if left <= local_x <= left + button.width():
                return True
        return False

    def _hit_test_edge(self, local: QPoint) -> int | None:
        rect = self.rect()
        margin = _RESIZE_MARGIN
        if not rect.adjusted(-margin, -margin, margin, margin).contains(local):
            return None
        left = local.x() <= margin
        right = local.x() >= rect.width() - margin
        top = local.y() <= margin
        bottom = local.y() >= rect.height() - margin
        if top and left:
            return _HTTOPLEFT
        if top and right:
            return _HTTOPRIGHT
        if bottom and left:
            return _HTBOTTOMLEFT
        if bottom and right:
            return _HTBOTTOMRIGHT
        if left:
            return _HTLEFT
        if right:
            return _HTRIGHT
        if top:
            return _HTTOP
        if bottom:
            return _HTBOTTOM
        return None

    # -- 模式切换 -------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    def _show_page(self, page: str) -> None:
        if page in (PAGE_PLUGINS, PAGE_CUSTOM) and self._run_thread is not None:
            # 运行中删模块会把正在用的插件进程抽走。
            self._notify("运行中，稍后再切页", "warning")
            return

        if page == PAGE_PLUGINS:
            self._mode = MODE_PLUGINS
            self.plugin_view.reload()
            self.stack.setCurrentIndex(1)
            self.sidebar.set_page(PAGE_PLUGINS)
        elif page == PAGE_CUSTOM:
            self._mode = MODE_CUSTOM
            self.custom_view.reload()
            self.stack.setCurrentIndex(3)
            self.sidebar.set_page(PAGE_CUSTOM)
        elif page == PAGE_CHECKLIST:
            self._mode = MODE_CHECKLIST
            self._refresh_checklist()
            self.stack.setCurrentIndex(5)
            self.sidebar.set_page(PAGE_CHECKLIST)
        else:
            self._mode = MODE_WORKFLOWS
            self.stack.setCurrentIndex(0)
            self.sidebar.set_page(PAGE_WORKFLOWS)
        self.sidebar.set_module_action_active(False)
        self.sidebar.set_checklist_action_active(False)
        self._apply_mode()

    def show_editor(self) -> None:
        self._mode = MODE_EDITOR
        self.stack.setCurrentIndex(2)
        self.sidebar.clear_page_selection()
        self.sidebar.set_module_action_active(False)
        self.sidebar.set_checklist_action_active(False)
        self._apply_mode()

    def _apply_mode(self) -> None:
        editing = self._mode == MODE_EDITOR

        # 侧边栏只在首页两页出现；编辑器里左边让给模块面板。
        self.sidebar.setVisible(not editing and self._sidebar_visible)
        # 工具栏只在编辑器里有内容可做（保存/运行/缩放/删除），首页的操作都在侧边栏。
        self.toolbar.setVisible(editing)
        self.palette_panel.setVisible(editing)
        self.inspector.setVisible(editing)
        self.log_panel.setVisible(self.action_show_log.isChecked())

        for action in (
            self.action_home,
            self.action_save,
            self.action_save_as,
            self.action_delete,
            self.action_run,
            self.action_zoom_in,
            self.action_zoom_out,
            self.action_zoom_reset,
            self.action_fit,
        ):
            action.setVisible(editing)
        # 「停止」两页都留着：在首页点运行之后，总得有个地方能把它停下来。
        self.action_stop.setVisible(True)
        self._rebalance_panels()
        self._update_title()

    def _rebalance_panels(self) -> None:
        """按当前该显示谁，重新分配分隔器的宽度。

        必须显式做这件事：``QSplitter`` 会把隐藏面板的尺寸记成 0，之后再 ``setVisible``
        回来，控件"可见"但宽度还是 0 —— ``isVisible()`` 返回 true，用户却什么都看不到。
        """
        total = sum(self.top_splitter.sizes()) or max(self.width(), 1200)
        sidebar = SIDEBAR_WIDTH if self.sidebar.isVisible() else 0
        palette = 270 if self.palette_panel.isVisible() else 0
        inspector = 330 if self.inspector.isVisible() else 0
        center = max(320, total - sidebar - palette - inspector)
        self.top_splitter.setSizes([sidebar, palette, center, inspector])

        # 首次布局之前分隔器的尺寸之和是不可靠的（可能只有几个像素），所以回落到窗口
        # 高度 —— 否则底边栏会被算成"没地方放"，然后被压到最小高度。
        big = max(sum(self.main_splitter.sizes()), self.height() - 60)
        # 用 isHidden() 而不是 isVisible()：_apply_mode() 在窗口 show() 之前就跑过一次，
        # 那时所有控件的 isVisible() 都是 False，用它会得出"日志面板是收起的"，把高度设成
        # 0 再被最小高度兜住 —— 于是初始高度永远等于最小值（实测 140），设多少都没用。
        # isHidden() 问的是"我们是不是明确收起了它"，和父窗口显示没显示无关。
        if not self.log_panel.isHidden():
            log = min(self._log_height, max(200, big - 200))
        else:
            # 收起之前先把高度记下来，展开时回到原样，而不是回到最小值。
            self._log_height = self.main_splitter.sizes()[1] or self._log_height
            log = 0
        self.main_splitter.setSizes([max(200, big - log), log])

    def _on_splitter_moved(self, _pos: int, index: int) -> None:
        """用户拖动分隔条之后记住底边栏的高度。"""
        sizes = self.main_splitter.sizes()
        if index == 1 and len(sizes) > 1 and sizes[1] > 0:
            self._log_height = sizes[1]

    def _on_zoom_changed(self, zoom: float) -> None:
        """缩放指示就印在工具栏那个按钮上 —— 省掉状态栏那一行，信息一点没少。"""
        self.action_zoom_reset.setText(f"{zoom * 100:.0f}%")

    def _toggle_log_panel(self, visible: bool) -> None:
        self.log_panel.setVisible(visible)
        if visible:
            sizes = self.main_splitter.sizes()
            if len(sizes) == 2 and sizes[1] < 80:
                self.main_splitter.setSizes([max(200, sizes[0] - 200), 200])

    def _install_dependencies(self, plugin_id: str, requirements: list[str]) -> None:
        """在后台把这条模块缺的包装上，输出流进运行日志。

        **只装到"这个模块跑起来会用的那个解释器"里。** 想当然用主 venv 的话，插件自带
        venv 时等于装到了别处 —— 用户看到"装好了"但模块照样起不来。
        """
        requirements = [r for r in requirements if r.strip()]
        if not requirements:
            return
        if self._install_thread is not None:
            self._notify("已经在装依赖了，等它跑完", "warning")
            return

        manifest = self.registry.manifests.get(plugin_id)
        target = target_python(manifest, sys.executable)
        self.log_panel.show_log_tab()
        self.log_panel.append_log(
            "info",
            f"给「{plugin_id}」装依赖（{describe_target(target)}）：{'、'.join(requirements)}",
        )

        thread = DependencyInstallThread(target, requirements, self)
        thread.line.connect(lambda text: self.log_panel.append_log("debug", text))
        thread.finished_with.connect(self._on_dependencies_installed)
        self._install_thread = thread
        self._set_install_busy(True)
        thread.start()

    def _set_install_busy(self, busy: bool) -> None:
        self.custom_view.set_install_enabled(not busy)

    def _on_dependencies_installed(self, ok: bool, message: str) -> None:
        self._install_thread = None
        self._set_install_busy(False)
        self.log_panel.append_log("info" if ok else "error", f"依赖安装：{message}")
        self._notify(f"依赖安装：{message}")

        # 重新发现一遍 —— 插件进程可能就是因为缺包才起不来的，装完该能起来了。
        self.registry.discover()
        self.palette_panel.rebuild()
        self.plugin_view.reload()
        self.custom_view.reload()
        self.workflow_list.reload()

    def _cancel_install(self) -> None:
        if self._install_thread is not None:
            self._install_thread.cancel()
            self.log_panel.append_log("warning", "已请求取消依赖安装")

    def _set_theme(self, light: bool) -> None:
        """切深色 / 浅色。

        光换 QSS 不够：各个页面在构造时把颜色**写进了自己的 setStyleSheet**，
        那些字符串不会跟着变。所以切完要把它们重建一遍 —— 反正每个页面本来就有
        reload()，重建是它们已经支持的操作。
        """
        theme.set_mode("light" if light else "dark")
        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app)

        self.workflow_list.reload()
        self.plugin_view.reload()
        self.custom_view.reload()
        self._refresh_checklist()
        self.palette_panel.rebuild()
        self.inspector.show_node(self.scene.selected_node())
        self.canvas_scene_repaint()
        self.title_bar.update()
        self.sidebar.update()
        self.log_panel.append_log("info", f"已切换到{'浅色' if light else '深色'}主题")

    def canvas_scene_repaint(self) -> None:
        """画布上的东西是自绘的，颜色在 paint 时才读 —— 让它重画一遍就行。"""
        self.scene.update()
        self.view.viewport().update()

    def _set_view_mode(self, mode: str) -> None:
        """只作用于**流程列表页**。

        页面右上角的切换按钮是各自独立的状态，这里同步的是流程页那一个（它还挂着
        Ctrl+1 / Ctrl+2，所以那个菜单项留着没意义，但快捷键还有用）。
        """
        self.workflow_list.set_view_mode(mode)

    def back_to_list(self) -> None:
        """回到流程列表。

        **这里要问"要不要保存"。** 编辑器里整个侧边栏是隐藏的（左边让给了模块面板），
        所以「返回」是**离开编辑状态的唯一出口** —— 一旦返回，再点「编辑」就是重新从磁盘
        读，手上的改动就没了。确认框放在这儿不是错位，它就是那个该做决定的时刻。

        编辑器里想直接开新的一条，走「文件 → 新建」或 Ctrl+N（菜单一直在，那边同样会问）。
        """
        if not self._confirm_discard():
            return
        self.workflow_list.reload()
        self._show_page(PAGE_WORKFLOWS)
        self._refresh_footer()
        self._notify("已返回流程列表")

    # -- 首页动作 -------------------------------------------------------------

    def run_entry(self, entry: WorkflowEntry) -> None:
        """从流程列表直接执行。"""
        if self._run_thread is not None:
            self._notify("已经有一条流程在运行了", "warning")
            return
        if not entry.ok:
            self._notify(f"{entry.path.name} 无法解析，先修好它", "warning")
            return

        try:
            workflow = load_workflow(entry.path)
        except MyAutoWorkError as exc:
            self.log_panel.append_log("error", f"无法读取 {entry.path.name}：{exc}")
            return

        self._run_entry_path = entry.path
        self._start_run(workflow, from_editor=False)

    def edit_entry(self, entry: WorkflowEntry) -> None:
        if self._run_thread is not None:
            self._notify("运行中，先停止再编辑", "warning")
            return
        if not self._confirm_discard():
            return
        if self.load_path(entry.path):
            self.show_editor()

    def reveal_entry(self, entry: WorkflowEntry) -> None:
        self._open_in_explorer(entry.path.parent)

    def _apply_ai_backend(self) -> None:
        """按当前设置决定 AI 面板用哪个后端。

        没配过就留着占位后端 —— 它会老老实实说"还没接模型，去「编辑 → 设置…」"，
        比换成一个连不上、只会报错的真后端友好。
        """
        if resolve_api_key(self.ai_settings):
            self.ai_panel.set_backend(
                ChatBackend(self.ai_settings),
                f"用 {self.ai_settings.model or '?'} · {self.ai_settings.chat_url}",
            )
            # 占位后端那句"现在只有框架，还没有接入模型"配上真后端就成了假话，换掉。
            self.ai_panel.reset(
                f"已连上 {self.ai_settings.model or '?'}。说说你想自动化什么 —— "
                "搭流程、写模块，或者贴一段报错让我看。"
            )
        else:
            self.ai_panel.set_backend(StubBackend(), "还没配置模型 —— 编辑 → 设置…")

    def _open_ai_settings(self) -> None:
        """打开 AI 设置。这个对话框**不该在流程运行时弹** —— 规则是运行期间不弹模态窗。"""
        if self._run_thread is not None:
            self._notify("运行中不能改设置，先停止", "warning")
            return

        dialog = SettingsDialog(self.ai_settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.log_panel.append_log("info", "已取消修改设置")
            return

        self.ai_settings = dialog.ai_settings()
        try:
            saved = save_ai_settings(self.ai_settings)
        except OSError as exc:
            # 存不下也别把设置丢掉 —— 这一次运行还是用新值，只是下次打开要重填。
            self.log_panel.append_log("error", f"AI 设置存不下来：{exc}")
            self._notify("设置存不下来，详见运行日志", "warning")
        else:
            self.log_panel.append_log("info", f"AI 设置已保存到 {saved}")

        self._apply_ai_backend()
        self.ai_panel.append_message(
            AiMessage(
                role="assistant",
                text=(
                    f"设置已更新，现在用 {self.ai_settings.model or '?'}"
                    if resolve_api_key(self.ai_settings)
                    else "设置已保存，但还没有 API Key —— 我不会回答任何问题。"
                ),
            )
        )
        self._notify("设置已更新")

    def _toggle_sidebar(self) -> None:
        """标题栏最左边那个按钮：收/放左侧栏。"""
        self._sidebar_visible = not self._sidebar_visible
        self._apply_mode()
        self._rebalance_panels()

    def _new_module(self) -> None:
        """新建模块：**直接切到右侧的代码界面**，不弹对话框。
        中间加一层"先填四个字段、点确定、然后才给代码框"只是多两步 —— 用户要的就是一个
        能写代码的地方。
        """
        if self._run_thread is not None:
            self._notify("运行中不能新建模块，先停止", "warning")
            # 按钮是可选中的，点了就会翻转 —— 被拦住时得把它按回原样。
            self.sidebar.set_module_action_active(self._mode == MODE_MODULE)
            return
        self.module_editor.start_new()
        self._mode = MODE_MODULE
        self.stack.setCurrentIndex(4)
        # 高亮的是操作区的「新建模块」，不是任何导航项 —— 编辑器是二级页面。
        self.sidebar.clear_page_selection()
        self.sidebar.set_module_action_active(True)
        self._apply_mode()
        self.module_editor.code.setFocus()

    def _edit_module(self, plugin_id: str) -> None:
        """打开一个已有模块改代码。内置的不给改。"""
        if self._run_thread is not None:
            self._notify("运行中不能改模块，先停止", "warning")
            return
        if self.registry.is_builtin(plugin_id):
            self._notify(f"{plugin_id} 是内置模块，不能改代码", "warning")
            return
        if not self.module_editor.load(plugin_id):
            self._notify(f"打不开 {plugin_id}", "warning")
            return

        self._mode = MODE_MODULE
        self.stack.setCurrentIndex(4)
        # 编辑已有模块也亮「新建模块」：走的是同一个界面、同一件事（写模块）。
        self.sidebar.clear_page_selection()
        self.sidebar.set_module_action_active(True)
        self._apply_mode()
        self.module_editor.code.setFocus()

    def _on_module_saved(self, plugin_id: str) -> None:
        """模块代码写盘成功：让所有引用它的地方跟上。"""
        self.registry.discover()
        self.palette_panel.rebuild()
        self.plugin_view.reload()
        self.custom_view.reload()
        self._refresh_footer()
        self._update_title()

        # 保存成功后从"新建"切成"编辑" —— 否则再点一次保存会因为重名而被拦住。
        self.module_editor.load(plugin_id)
        self.log_panel.append_log("info", f"已保存模块 {plugin_id}（{self.plugins_dir / plugin_id}）")
        self._notify(f"已保存模块 {plugin_id}")

    def _on_module_save_failed(self, reason: str) -> None:
        self.log_panel.append_log("error", f"模块保存失败：{reason}")
        self._notify("模块保存失败，详见运行日志", "warning")

    def _open_module(self, plugin_id: str) -> None:
        """跳到「我的模块」页看这个模块。"""
        self._show_page(PAGE_CUSTOM)

    def _rename_module(self, plugin_id: str) -> None:
        """改显示名。**标识不动** —— 改了标识，用到这个模块的流程会全部失效。"""
        manifest = self.registry.manifests.get(plugin_id)
        current = manifest.name if manifest is not None else plugin_id

        new_name, accepted = QInputDialog.getText(
            self, "重命名模块", "新的显示名（标识不变）：", text=current
        )
        new_name = new_name.strip()
        if not accepted or not new_name or new_name == current:
            return

        try:
            self.registry.rename(plugin_id, new_name)
        except (MyAutoWorkError, OSError) as exc:
            self.log_panel.append_log("error", f"重命名 {plugin_id} 失败：{exc}")
            self._notify("重命名失败，详见运行日志", "warning")
            return

        self.palette_panel.rebuild()
        self.plugin_view.reload()
        self.custom_view.reload()
        self.log_panel.append_log(
            "info", f"模块 {plugin_id} 改名为 {new_name}（标识没动，流程不受影响）"
        )
        self._notify(f"已重命名为 {new_name}")

    def _reveal_path(self, path: str) -> None:
        """在资源管理器里打开某个目录。"""
        target = Path(path)
        if not target.exists():
            self._notify(f"目录不存在：{target}", "warning")
            return
        self._open_in_explorer(target)

    def _open_in_explorer(self, target: Path) -> None:
        """在资源管理器里打开一个目录。

        **不用 ``QDesktopServices.openUrl``。** 它对**目录**不一定成功，而且失败时只是
        返回 False —— 原先把返回值丢掉了，用户点了没反应，日志里也没有任何线索，只能干瞪眼。
        Windows 上 ``os.startfile`` 就是"双击这个文件夹"，最直接；失败会抛异常，我们能记下来。

        非 Windows 上退回 Qt 的实现，但**这次检查返回值**。
        """
        path = Path(target)
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                raise OSError("系统里没有能打开目录的处理程序")
        except OSError as exc:
            self.log_panel.append_log("error", f"打不开目录 {path}：{exc}")
            self._notify(f"打不开目录：{exc}", "warning")
            return
        self.log_panel.append_log("info", f"已打开 {path}")

    def _reveal_plugins_dir(self) -> None:
        self._open_in_explorer(self.plugins_dir)

    def _reveal_workflows_dir(self) -> None:
        self._open_in_explorer(self.workflows_dir)

    def _delete_workflow(self, entry: Any) -> None:
        """把一条流程删到回收站。

        **正跑着的那条不许删。** 引擎后面还要按路径读它 —— 与其让它在中途炸掉，不如拦住。
        """
        if entry is None:
            return
        if (
            self._run_thread is not None
            and self._run_entry_path is not None
            and Path(self._run_entry_path) == Path(entry.path)
        ):
            self._notify("这条流程正在运行，先停止再删", "warning")
            return

        answer = QMessageBox.question(
            self,
            "删除流程",
            f"确定要删除「{entry.display_name}」吗？\n\n"
            f"{entry.path}\n\n"
            "会删到**回收站**，误删可以从那里还原。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            removed = self.library.remove(entry.path)
        except (MyAutoWorkError, OSError) as exc:
            self.log_panel.append_log("error", f"删除流程失败：{exc}")
            self._notify("删除失败，详见运行日志", "warning")
            return

        # 画布不记得"我开的是哪个文件"，所以删完没法判断该不该清空它。稳妥的做法是退回
        # 列表页 —— 留在一个可能指向已删文件的编辑器里，用户下次一保存就又把它写回来了。
        if self._mode == MODE_EDITOR:
            self._show_page(PAGE_WORKFLOWS)

        self.workflow_list.reload()
        self._refresh_footer()
        self.log_panel.append_log(
            "info", f"已删除流程「{entry.display_name}」（{removed}，在回收站里）"
        )
        self._notify(f"已删除「{entry.display_name}」，回收站里可以还原")

    def _open_web_tool(self) -> None:
        """打开网页工具窗口。

        **允许在运行时开着**（它本身不跑流程，只是问插件要数据），但**运行中不给开** ——
        那时候插件的调用线程被引擎占着，工具窗口的轮询会排在后面等，
        表现是"点了没反应"。
        """
        if self._run_thread is not None:
            self._notify("运行中不能开网页工具，先停止", "warning")
            return
        if self._web_tool is None:
            self._web_tool = WebToolWindow(self.registry, self.workdir, self)
        self._web_tool.show()
        self._web_tool.raise_()
        self._web_tool.activateWindow()

    def _reveal_plugin(self, plugin_id: str) -> None:
        manifest = self.registry.manifests.get(plugin_id)
        if manifest is None:
            self._notify(f"找不到插件 {plugin_id}", "warning")
            return
        self._open_in_explorer(manifest.path)

    def _show_plugin_details(self, plugin_id: str) -> None:
        entry = next((p for p in self.registry.catalog() if p["id"] == plugin_id), None)
        if entry is None:
            self._notify(f"找不到插件 {plugin_id}", "warning")
            return
        PluginDetailsDialog(entry, self).exec()

    def _install_plugin_package(self) -> None:
        """装一个插件压缩包 —— **先看后装**。

        ``inspect()`` 只读不写，把包里的情况（插件 id、名字、版本、文件数、会不会覆盖）
        摆出来让用户确认；``install()`` 才动磁盘。顺序反过来（先装再问）等于没得选。
        """
        if self._run_thread is not None:
            self._notify("运行中不能装插件，先停止", "warning")
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "选一个插件包", str(self.workdir), "插件包 (*.zip);;所有文件 (*)"
        )
        if not path:
            return

        try:
            info = plan_package(inspect_package(Path(path)), self.plugins_dir)
        except PluginPackageError as exc:
            self.log_panel.append_log("error", f"这个包装不了：{exc}")
            self._notify("这不是一个能装的插件包，详见运行日志", "warning")
            return

        answer = QMessageBox.question(
            self,
            "安装插件包",
            f"{info.describe()}\n\n确定装吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.log_panel.append_log("info", f"已取消安装 {info.plugin_id}")
            return

        try:
            target = install_package(info, self.plugins_dir, overwrite=info.conflict)
        except PluginPackageError as exc:
            self.log_panel.append_log("error", f"安装失败：{exc}")
            self._notify("安装失败，详见运行日志", "warning")
            return

        self.registry.discover()
        self.palette_panel.rebuild()
        self.plugin_view.reload()
        self.custom_view.reload()
        self.workflow_list.reload()
        self._refresh_footer()
        self.log_panel.append_log(
            "info",
            f"已安装插件 {info.plugin_id}（{info.name} v{info.version}）-> {target}"
            + ("（覆盖了原来的）" if info.conflict else ""),
        )
        self._notify(f"已安装 {info.name}")

    def _delete_plugin(self, plugin_id: str) -> None:
        """删除一个插件（连同它的文件夹），**删到回收站**。

        内置插件也能删（``force=True``），但对话框里会**明说它是内置的**：流程按 id 引用
        插件，删掉一个内置的会让用到它的流程全部失效。走回收站让这一步可以后悔，但"哪些流程
        受影响"必须**删之前**就摆出来，而不是等用户去画布上一个个发现。

        运行中不许删 —— 插件进程正被引擎用着，抽掉会得到一个半死不活的运行。
        """
        if self._run_thread is not None:
            self._notify("运行中不能删除插件，先停止", "warning")
            return

        manifest = self.registry.manifests.get(plugin_id)
        builtin = self.registry.is_builtin(plugin_id)
        name = f"{manifest.name}（{plugin_id}）" if manifest is not None else plugin_id

        used = workflows_using(self.workflows_dir, plugin_id)
        detail = ""
        if used:
            preview = "、".join(used[:5]) + ("…" if len(used) > 5 else "")
            detail += (
                f"\n\n注意：有 {len(used)} 条流程在用它（{preview}）。"
                "删掉之后那些节点会变成「插件未安装」。"
            )
        if builtin:
            detail += "\n\n这是程序**自带**的插件 —— 删掉之后这部分能力就没了。"

        answer = QMessageBox.question(
            self,
            "删除内置插件" if builtin else "删除插件",
            f"确定要删除 {name} 吗？\n\n"
            f"会连同它的文件夹一起删掉，但**删到回收站**，误删可以从那里还原。{detail}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.log_panel.append_log("info", f"已取消删除 {plugin_id}")
            return

        try:
            removed = self.registry.remove(plugin_id, force=builtin)
        except (MyAutoWorkError, OSError) as exc:
            self.log_panel.append_log("error", f"删除 {plugin_id} 失败：{exc}")
            self._notify("删除失败，详见运行日志", "warning")
            return

        # 删完要把所有还在引用它的地方刷一遍：模块面板、插件页、自定义页、首页。
        self.palette_panel.rebuild()
        self.plugin_view.reload()
        self.custom_view.reload()
        self.workflow_list.reload()
        self._refresh_footer()

        self.log_panel.append_log("info", f"已删除插件 {plugin_id}（{removed}，在回收站里）")
        if used:
            self.log_panel.append_log(
                "warning",
                f"  {len(used)} 条流程还在引用它，现在会显示「插件未安装」",
            )
        self._notify(f"已删除插件 {plugin_id}，回收站里可以还原")

    def _save_plugin_draft(self, draft: Any) -> None:
        """把 AI 助手（或以后其它来源）的插件草稿落盘。

        先校验再写：插件是能读写文件、能操作界面的代码，宁可拒绝也不能写一个半坏的进去
        —— 那会把整个注册表带崩。
        """
        problems = draft.validate()
        if problems:
            for problem in problems:
                self.log_panel.append_log("error", f"插件草稿有问题：{problem}")
            return

        try:
            path = draft.write(self.plugins_dir)
        except (MyAutoWorkError, OSError) as exc:
            self.log_panel.append_log("error", f"插件写入失败：{exc}")
            return

        # 重新发现一遍，新插件立刻出现在模块面板和插件页里。
        self.registry.discover()
        self.palette_panel.rebuild()
        self.plugin_view.reload()
        self.custom_view.reload()
        self._refresh_footer()

        self.log_panel.append_log("info", f"已创建插件 {draft.plugin_id} -> {path}")
        self.log_panel.append_log(
            "info",
            f"  拖到画布上就能用：{draft.plugin_id}/{draft.provides.get('actions', ['?'])[0]}",
        )

    def _refresh_all(self) -> None:
        self.workflow_list.reload()
        self.plugin_view.reload()
        self.custom_view.reload()
        self._refresh_checklist()
        self._refresh_footer()
        # 状态栏只报"这次刷新动了什么"，不重复报插件数。
        self._notify(f"已刷新：{len(self.library.entries)} 条流程")

    def _refresh_footer(self) -> None:
        """刷新左侧栏里所有"由注册表/工作流库派生"的东西。

        底部统计和模块列表是一回事：都是把当前状态映到侧边栏上。放在一起，是为了
        避免出现"加了模块但侧边栏没跟上"这种只在一半入口里忘了刷新才发生的 bug。
        """
        self.sidebar.set_footer(f"{len(self.library.entries)} 条流程")

    # -- 文档 -----------------------------------------------------------------

    def _update_title(self) -> None:
        # 软件名暂时不显示，窗口标题只用来说明"现在在哪一页"。
        if self._mode == MODE_EDITOR:
            name = self.current_path.name if self.current_path else self.scene.workflow_name
            mark = " *" if self._dirty else ""
            self.setWindowTitle(f"{name}{mark}")
        elif self._mode == MODE_PLUGINS:
            self.setWindowTitle("我的插件")
        elif self._mode == MODE_CUSTOM:
            self.setWindowTitle("我的模块")
        elif self._mode == MODE_CHECKLIST:
            self.setWindowTitle("我的清单")
        elif self._mode == MODE_CHECKLIST_EDIT:
            editing = self.checklist_editor.editing
            self.setWindowTitle(f"编辑清单 · {editing}" if editing else "新建清单")
        elif self._mode == MODE_MODULE:
            editing = self.module_editor.editing
            self.setWindowTitle(f"编辑模块 · {editing}" if editing else "新建模块")
        else:
            self.setWindowTitle("我的流程")

    def _mark_dirty(self) -> None:
        if self._loading_document or self._dirty:
            return
        self._dirty = True
        self._update_title()

    def _on_scene_changed(self) -> None:
        if not self._loading_document:
            self._mark_dirty()
        self.inspector.refresh_upstream()

    def add_node_at_center(self, plugin: str, action: str) -> None:
        center = self.view.mapToScene(self.view.viewport().rect().center())
        item = self.scene.add_node(
            plugin,
            action,
            QPointF(center.x() - theme.NODE_WIDTH / 2.0, center.y() - 40.0),
        )
        self.scene.clearSelection()
        item.setSelected(True)
        self._notify(f"已添加 {plugin}/{action}")

    def new_workflow(self) -> None:
        # 从「文件 → 新建」或 Ctrl+N 走这条路。侧边栏在编辑器里是隐藏的，所以这是编辑
        # 途中想直接开新一条的入口 —— 它和「返回」一样会把当前这份丢掉，因此同样要问。
        if not self._confirm_discard():
            return
        self._loading_document = True
        self.scene.load_workflow(_blank_workflow())
        self._loading_document = False
        self.current_path = None
        self._dirty = False
        self.show_editor()
        self.view.fit_content()
        self.inspector.show_node(None)
        self.log_panel.clear_log()
        self._notify("已新建工作流，编辑完记得保存")

    def open_workflow(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "打开工作流", str(self.workflows_dir), "工作流 (*.json);;所有文件 (*)"
        )
        if path:
            if self.load_path(Path(path)):
                self.show_editor()

    def load_path(self, path: str | Path) -> bool:
        file_path = Path(path)
        try:
            workflow = load_workflow(file_path)
        except MyAutoWorkError as exc:
            QMessageBox.warning(self, "打开失败", str(exc))
            return False

        self._loading_document = True
        warnings = self.scene.load_workflow(workflow)
        self._loading_document = False

        self.current_path = file_path
        self._dirty = False
        self._update_title()
        self.view.fit_content()
        self.inspector.show_node(None)
        self.log_panel.clear_log()

        for warning in warnings:
            self.log_panel.append_log("warning", warning)
        message = f"已打开 {file_path.name}"
        if warnings:
            message += f"，{len(warnings)} 处无法还原（见日志）"
        self.log_panel.append_log("info", message)
        return True

    def save_workflow(self) -> bool:
        if self.current_path is None:
            return self.save_workflow_as()
        return self._write(self.current_path)

    def save_workflow_as(self) -> bool:
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        default = str(self.current_path or (self.workflows_dir / f"{self.scene.workflow_id}.json"))
        path, _ = QFileDialog.getSaveFileName(self, "保存工作流", default, "工作流 (*.json)")
        if not path:
            return False
        return self._write(Path(path))

    def _write(self, path: Path) -> bool:
        workflow = self.scene.to_workflow()
        try:
            save_workflow(workflow, path)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return False
        self.current_path = path
        self._dirty = False
        self._update_title()
        self.log_panel.append_log("info", f"已保存 {path}")
        # 首页要能立刻看到这条（新建的或改过名字的）。
        self.workflow_list.reload()
        self._refresh_footer()
        return True

    def _confirm_discard(self) -> bool:
        """有未保存改动时问一句。返回 True 表示"可以继续"。

        **用户选了放弃之后必须把 ``_dirty`` 清掉。** 否则会出现这样一串：在编辑器里改了点
        东西 -> 点返回 -> 被问一次 -> 选"不保存" -> 回到列表 -> 再点「新建流程」-> **又被
        问一遍同一件事**。而那次改动其实早就丢了，第二次问纯属骚扰 —— 用户会以为第一次
        没生效。
        """
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self,
            "尚未保存",
            "当前工作流有未保存的改动，要先保存吗？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            # 保存成功的话 _write 自己会清 _dirty；保存失败返回 False，改动还在。
            return self.save_workflow()

        # 放弃：改动已经明确不要了，别再问第二遍。
        self._dirty = False
        self._update_title()
        return True

    # -- 运行 -----------------------------------------------------------------

    def run_current_workflow(self) -> None:
        if self._run_thread is not None:
            self._notify("已经在运行了", "warning")
            return
        self._run_entry_path = None
        self._start_run(self.scene.to_workflow(), from_editor=True)

    def _start_run(self, workflow: Workflow, *, from_editor: bool) -> None:
        problems = workflow.validate(self.registry)
        errors = [p for p in problems if p.is_error]

        self.log_panel.show_log_tab()
        if errors:
            self.log_panel.append_log("error", f"校验未通过，共 {len(errors)} 个错误：")
            for problem in errors:
                self.log_panel.append_log("error", str(problem))
            self._run_entry_path = None
            return
        for problem in problems:
            self.log_panel.append_log("warning", str(problem))

        self._run_from_editor = from_editor
        if from_editor:
            self.scene.reset_run_state()
        else:
            self.workflow_list.set_running(self._run_entry_path)

        source = "" if from_editor else f"（来自流程列表：{self._run_entry_path.name if self._run_entry_path else ''}）"
        self.log_panel.append_log(
            "info", f"开始运行「{workflow.name}」，{len(workflow.nodes)} 个节点{source}"
        )
        self._set_running(True)

        thread = WorkflowRunThread(
            self.registry,
            self.store,
            workflow,
            workdir=self.workdir,
            timeout=self.timeout,
            parent=self,
        )
        thread.eventOccurred.connect(self._on_run_event)
        thread.runFinished.connect(self._on_run_finished)
        thread.finished.connect(self._on_thread_done)
        self._run_thread = thread
        thread.start()

    def stop_workflow(self) -> None:
        if self._run_thread is None:
            return
        self._run_thread.cancel()
        self.action_stop.setEnabled(False)
        self.log_panel.append_log("warning", "已请求停止，当前节点执行完后会停下来")

    def _set_running(self, running: bool) -> None:
        self.action_run.setEnabled(not running)
        self.action_stop.setEnabled(running)
        self.action_open.setEnabled(not running)
        self.action_new.setEnabled(not running)
        # 跑的时候别让刷新把列表重建了 —— 界面上的"运行中"标记会跟着丢。
        self.action_refresh.setEnabled(not running)
        # 运行中不许删模块：插件进程正被引擎用着。
        self.custom_view.set_delete_enabled(not running)
        self.sidebar.set_enabled_actions(not running)

    def _refresh_inspector_if_current(self, node_id: str) -> None:
        """跑完一个节点之后，如果属性面板正开着它，就把「上次输出」刷新出来。

        **不刷新的话要切走再切回来才看得到** —— 而"跑一遍、看它给了什么"是最常见的
        排错动作，多一步切换就足够让人以为这个功能不存在。
        """
        if self.inspector.current_node_id != node_id:
            return
        item = self.scene.node_item(node_id)
        if item is not None:
            self.inspector.show_node(item)

    def _on_run_event(self, kind: str, payload: dict[str, Any]) -> None:
        # 从流程列表跑的时候画布上可能是另一条流程，不能去改它的显示。
        if self._run_from_editor:
            if kind == "node_started":
                self.scene.mark_node_started(payload["node_id"])
            elif kind == "node_finished":
                self.scene.mark_node_finished(
                    payload["node_id"],
                    duration_ms=int(payload.get("duration_ms") or 0),
                    ok=True,
                    outputs=payload.get("outputs") or {},
                )
                self._refresh_inspector_if_current(payload["node_id"])
            elif kind == "node_failed":
                self.scene.mark_node_failed(payload["node_id"], str(payload.get("error") or ""))
                self._refresh_inspector_if_current(payload["node_id"])
            elif kind == "node_skipped":
                self.scene.mark_node_skipped(payload["node_id"])

        if kind == "log":
            self.log_panel.append_log(
                str(payload.get("level") or "info"),
                str(payload.get("message") or ""),
                str(payload.get("node_id") or ""),
            )
        elif kind == "variable_changed":
            self.log_panel.set_variable(str(payload.get("name") or ""), payload.get("value"))

    def _on_run_finished(self, result: Any) -> None:
        if result is None:
            self.log_panel.append_log("error", "运行线程异常结束，没有拿到结果")
            return

        verb = {"success": "成功", "failed": "失败", "cancelled": "已停止"}.get(result.status, result.status)
        level = "info" if result.ok else ("warning" if result.cancelled else "error")
        self.log_panel.append_log(
            level, f"运行结束：{verb}，耗时 {result.duration_ms / 1000:.2f}s（{result.run_id}）"
        )
        if result.error:
            self.log_panel.append_log("error", result.error)

        self.log_panel.set_variables(result.variables)
        self.log_panel.refresh_history()
        self._last_run_status = result.status

        # 首页那条卡片要显示新的"上次运行结果"。
        if self._run_entry_path is not None:
            entry = self.library.refresh_entry(self._run_entry_path)
            if entry is not None:
                self.workflow_list.update_entry(entry)
        self._run_entry_path = None

    def _on_thread_done(self) -> None:
        self._run_thread = None
        self._set_running(False)
        self.workflow_list.set_running(None)

        # 清单模式下，一条跑完就接着下一条。放在这里而不是 _on_run_finished，是因为
        # 那时 _run_thread 还没清掉，下一条会被"已经在运行了"挡住。
        if self._checklist_queue:
            self._step_checklist()

    # -- 清单 -----------------------------------------------------------------

    def _checklist_path(self) -> Path:
        """清单文件放 ``<workflows 的同级>/checklists/checklists.json``。

        锚在 **workflows 目录**上，不是工作目录 —— 后者是启动时所在的地方，换个目录启动
        就会指向另一个文件。老位置的文件会被搬过来：位置变了，用户的清单不该跟着丢。
        """
        return resolve_checklist_path(self.workflows_dir, self.workdir)

    def _persist_checklists(self) -> None:
        save_checklists(
            self._checklist_path(),
            self._checklists,
            self._current_checklist,
            self._checklist_settings_map,
        )

    def _refresh_checklist(self) -> None:
        """从磁盘读回所有清单，把列表和"当前是哪条"都刷新一遍。"""
        data = load_checklist_file(self._checklist_path())
        self._checklists = dict(data.checklists)
        self._checklist_settings_map = dict(data.settings)

        # **不自动建一条「默认清单」。** 一条都没有时就是空的 —— 页面上有明确的空状态提示
        # （"还没有清单。点右上角「新建清单」建一条"）。凭空冒出一条用户没建过的东西，
        # 比空着更让人困惑：他会以为那是自己建的，或者以为必须留着。
        current = data.current
        if current not in self._checklists:
            current = sorted(self._checklists)[0] if self._checklists else ""
        self._current_checklist = current
        self.checklist_view.set_checklists(
            [(name, len(items)) for name, items in sorted(self._checklists.items())], current
        )

    # -- 清单编辑（单独一页） --------------------------------------------------

    def _open_checklist_editor(self, name: str) -> None:
        """打开清单编辑器。``name`` 为空表示新建。"""
        entries = self.library.filtered("")
        if name:
            self.checklist_editor.load(name, entries, set(self._checklists.get(name, [])))
        else:
            self.checklist_editor.start_new(entries)

        self._mode = MODE_CHECKLIST_EDIT
        self.stack.setCurrentIndex(6)
        self.sidebar.clear_page_selection()
        # 点亮来路那个按钮。清单编辑器和模块编辑器一样是**二级页面**：左侧没有任何导航项
        # 处于选中状态，不把「新建清单」点亮的话，用户进来会觉得自己掉进了一个不知所谓的
        # 页面，也看不出是从哪儿来的。
        self.sidebar.set_checklist_action_active(True)
        self._apply_mode()
        self.checklist_editor.focus_name()

    def _begin_new_checklist(self) -> None:
        """操作列 / 列表页的「新建清单」：**开一个独立页面**，不是就地弹输入行。"""
        if self._run_thread is not None:
            self._notify("运行中不能新建清单，先停止", "warning")
            return
        self._open_checklist_editor("")

    def _edit_checklist(self, name: str) -> None:
        if self._run_thread is not None:
            self._notify("运行中不能改清单，先停止", "warning")
            return
        if name not in self._checklists:
            return
        self._open_checklist_editor(name)

    def _rename_checklist(self, name: str) -> None:
        # 改名也在编辑器里做 —— 顺手就能一起改内容，不用为改一个名字再弹一个窗口。
        self._edit_checklist(name)

    def _on_checklist_saved(self, old: str, new: str, picked: list[str]) -> None:
        """编辑器点了保存。"""
        new = new.strip()
        if not new:
            return

        # 重名检查：新建时不能撞已有的；改名时不能撞别的（改成自己不算撞）。
        if new in self._checklists and new != old:
            self.checklist_editor.show_name_error(f"已经有叫「{new}」的清单了，换个名字")
            return

        if old and old != new:
            # 重建字典而不是改 key 再删 —— dict 保序，重建更不容易漏。
            self._checklists = {
                (new if key == old else key): value for key, value in self._checklists.items()
            }
        self._checklists[new] = list(picked)
        self._current_checklist = new
        self._persist_checklists()

        action = f"已更新清单「{new}」" if old else f"已新建清单「{new}」"
        self.log_panel.append_log("info", f"{action}（{len(picked)} 条流程）")
        self._notify(action)

        self._show_page(PAGE_CHECKLIST)

    def _checklist_settings(self, name: str) -> None:
        """一条清单的设置。现在只有「开机运行」。

        这里要同时维护**两处状态**：``checklists.json`` 里的标志，和**注册表里的启动项**。
        而且以注册表为准 —— 用户手动删掉启动项、或者换了机器，文件里的 true 就是假的。
        """
        if name not in self._checklists:
            return
        if self._run_thread is not None:
            self._notify("运行中不能改清单设置，先停止", "warning")
            return

        current = self._checklist_settings_map.get(name, ChecklistSettings())
        dialog = ChecklistSettingsDialog(name, current, startup.available(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        want = dialog.startup_enabled()
        try:
            if want:
                command, boot = startup.launch_command(
                    name,
                    repo=REPO_ROOT,
                    workdir=self.workdir,
                    workflows_dir=self.workflows_dir,
                    plugins_dir=self.plugins_dir,
                )
                startup.set_startup(name, command)
                self.log_panel.append_log(
                    "info",
                    f"清单「{name}」已设为开机运行。引导文件：{boot}\n"
                    f"  启动项：HKCU\\{startup.RUN_KEY} → {startup.NAME_PREFIX}{name}",
                )
            else:
                startup.set_startup(name, None)
                self.log_panel.append_log("info", f"清单「{name}」已取消开机运行")
        except startup.StartupUnavailable as exc:
            self._notify(str(exc), "warning")
            return
        except OSError as exc:
            self.log_panel.append_log("error", f"改开机启动项失败：{exc}")
            self._notify("改开机启动项失败，详见运行日志", "warning")
            return

        self._checklist_settings_map[name] = ChecklistSettings(startup=want)
        self._persist_checklists()
        self._refresh_checklist()
        self._notify(f"「{name}」开机运行已{'开启' if want else '关闭'}")

    def _delete_checklist(self, name: str) -> None:
        if name not in self._checklists:
            return
        if self._run_thread is not None:
            self._notify("运行中不能删除清单，先停止", "warning")
            return

        answer = QMessageBox.question(
            self,
            "删除清单",
            f"确定删除清单「{name}」吗？\n\n只删这份清单，流程本身不会被删。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        # 删之前先把开机运行关掉。**不清的话注册表里会留一个指向已删清单的启动项** ——
        # 每次开机都白跑一次，用户在界面里再也找不到它，因为那条清单已经没了。
        if self._checklist_settings_map.get(name, ChecklistSettings()).startup:
            try:
                startup.set_startup(name, None)
                self.log_panel.append_log("info", f"  同时取消了「{name}」的开机运行")
            except (startup.StartupUnavailable, OSError) as exc:
                self.log_panel.append_log("warning", f"  取消开机运行失败：{exc}")

        self._checklists.pop(name, None)
        self._checklist_settings_map.pop(name, None)
        if self._current_checklist == name:
            self._current_checklist = sorted(self._checklists)[0] if self._checklists else ""
        self._persist_checklists()
        self._refresh_checklist()
        self.log_panel.append_log("info", f"已删除清单「{name}」（流程本身没动）")

    # -- 清单执行 -------------------------------------------------------------

    def _run_checklist(self, name: str) -> None:
        """按清单顺序跑。**严格串行**，不是并发。"""
        if name not in self._checklists:
            return
        if self._run_thread is not None:
            self._notify("已经有一条流程在运行了", "warning")
            return

        names = list(self._checklists[name])
        if not names:
            self._notify(f"清单「{name}」还是空的", "warning")
            return

        queue = [self.workflows_dir / item for item in names]
        missing = [p.name for p in queue if not p.is_file()]
        if missing:
            self._notify(f"清单里有找不到的流程，已跳过：{'、'.join(missing)}", "warning")
            queue = [p for p in queue if p.is_file()]
        if not queue:
            return

        self._current_checklist = name
        self._checklist_queue = queue
        self._checklist_index = -1
        self._checklist_failed = []
        self._checklist_running = name
        self.checklist_view.clear_marks()
        self.checklist_view.set_running(name)
        self.log_panel.append_log(
            "info", f"开始执行清单「{name}」：{len(queue)} 条流程，按顺序跑（不是并发）"
        )
        self._advance_checklist()

    def _advance_checklist(self) -> None:
        self._checklist_index += 1
        if self._checklist_index >= len(self._checklist_queue):
            self._finish_checklist()
            return

        path = self._checklist_queue[self._checklist_index]
        try:
            workflow = load_workflow(path)
        except MyAutoWorkError as exc:
            self.log_panel.append_log("error", f"清单：{path.name} 读不出来，跳过（{exc}）")
            self._checklist_failed.append(path.name)
            # 用定时器排下一次，别在回调里直接递归 —— 一整队坏文件会把自己叠爆栈。
            QTimer.singleShot(0, self._advance_checklist)
            return

        self._run_entry_path = path
        self._start_run(workflow, from_editor=False)

    def _step_checklist(self) -> None:
        """上一条跑完了：记下结果，然后决定是继续还是收工。"""
        index = self._checklist_index
        if 0 <= index < len(self._checklist_queue) and self._last_run_status == "cancelled":
            # 用户按了停止 —— 整批都停下，而不是"停一条、跑下一条"。
            self.log_panel.append_log("warning", "清单已被停止，剩下的不再执行")
            self.checklist_view.mark(self._checklist_running, "", "已停止")
            self._checklist_queue = []
            self._checklist_running = ""
            self.checklist_view.set_running(None)
            self._notify("清单已停止")
            return

        if 0 <= index < len(self._checklist_queue):
            path = self._checklist_queue[index]
            if self._last_run_status != "success":
                self._checklist_failed.append(path.name)

        self._advance_checklist()

    def _finish_checklist(self) -> None:
        total = len(self._checklist_queue)
        failed = list(self._checklist_failed)
        name = self._checklist_running
        self._checklist_queue = []
        self._checklist_running = ""
        self.checklist_view.set_running(None)

        ok = total - len(failed)
        self.checklist_view.mark(
            name, "success" if not failed else "failed", f"{ok}/{total} 成功"
        )
        text = f"清单「{name}」执行完毕：{ok}/{total} 成功"
        if failed:
            text += f"，失败 {len(failed)} 条（{'、'.join(failed)}）"
        self.log_panel.append_log("warning" if failed else "info", text)
        self._notify(text)

    # -- 其它 -----------------------------------------------------------------

    def _on_pick_requested(self, port: str) -> None:
        """属性面板上的 🎯 被按下了。"""
        if self.scene.selected_node() is None:
            self._notify("先在画布上选中一个节点，再点 🎯 拾取元素", "warning")
            return
        self._pick_port = port
        started = self.picker.start(port)
        self.log_panel.append_log(
            "info",
            f"开始拾取「{port}」：把鼠标移到目标上，然后点拾取面板上的「采集」",
        )
        if started:
            self._notify("拾取模式已开启，移动到目标后点「采集」")

    def _on_element_picked(self, port: str, locator: Any) -> None:
        item = self.scene.selected_node()
        if item is None:
            self._notify("拾取完成，但当前没有选中节点", "warning")
            return

        self.inspector.set_element(port, locator)
        self.log_panel.append_log(
            "info",
            f"已拾取「{port}」：{locator.describe()}",
        )
        self.log_panel.append_log(
            "info" if not locator.is_fragile else "warning",
            f"  定位级别：{locator.describe_strategies()}",
        )
        if locator.is_fragile:
            self.log_panel.append_log(
                "warning",
                "  这条定位只靠图像/坐标，界面一变就会失效 —— 尽量拾取有 AutomationId 的控件",
            )
        self._notify(f"已拾取元素：{locator.describe()}")

    def _on_pick_cancelled(self) -> None:
        self.log_panel.append_log("info", "已取消拾取")

    def _on_worker_stderr(self, line: str) -> None:
        self.log_panel.append_log("debug", line)

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            f"关于 {APP_NAME}",
            f"{APP_NAME} —— 以插件为核心的 Windows 自动化运行时\n\n"
            "内核 · 插件契约 · 执行流 + 数据流双线引擎\n"
            f"已加载 {len(self.registry.manifests)} 个插件\n"
            f"流程目录 {self.workflows_dir}",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._run_thread is not None:
            QMessageBox.information(self, "正在运行", "还有流程在运行，请先停止再退出。")
            event.ignore()
            return
        if self.confirm_on_close and self._mode == MODE_EDITOR and not self._confirm_discard():
            event.ignore()
            return
        self.registry.shutdown()
        if self.store is not None:
            try:
                self.store.close()
            except Exception:  # pragma: no cover
                pass
        event.accept()


def _blank_workflow() -> Workflow:
    workflow = Workflow(id="untitled", name="未命名工作流")
    # 新工作流给一个起点，否则用户面对的是一张空画布、不知道从哪开始。
    workflow.nodes["n1"] = Node(
        id="n1",
        plugin="core.trigger.manual",
        action="start",
        pos=(80.0, 200.0),
    )
    return workflow
