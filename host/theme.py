"""画布与面板的视觉常量。

集中在一处的原因很实际：节点配色、端口配色、行高这些数字会被画布、端口、连线、
概览图、截图脚本同时用到，散在各文件里改一处忘一处。

**端口颜色 = 数据类型**，这条映射是刻意做的：用户扫一眼画布就能看出哪根线是文本、
哪根是元素、哪根是执行流。连线报错时也更容易反应过来"我把数字连到元素上了"。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #

NODE_WIDTH = 240.0
#: 表头要装下两行（标题 + 插件/动作），34 像素会把副标题的下半截切掉。
NODE_HEADER_HEIGHT = 46.0
NODE_ROW_HEIGHT = 24.0
NODE_PADDING_TOP = 8.0
NODE_PADDING_BOTTOM = 12.0
NODE_RADIUS = 8.0

PORT_RADIUS = 5.5
PORT_HIT_RADIUS = 11.0

EDGE_ARROW_SIZE = 9.0
EDGE_WIDTH_DATA = 1.8
EDGE_WIDTH_EXEC = 2.4

GRID_SIZE = 22.0

MIN_ZOOM = 0.25
MAX_ZOOM = 2.5

# --------------------------------------------------------------------------- #
# 颜色
# --------------------------------------------------------------------------- #

BACKGROUND = "#1e2228"
GRID_LINE = "#272c34"
GRID_LINE_MAJOR = "#2f3540"

NODE_BODY = "#2b3138"
NODE_BODY_SELECTED = "#333b45"
NODE_BORDER = "#3d4550"
NODE_BORDER_SELECTED = "#5b9dd9"
NODE_TEXT = "#dfe4ea"
NODE_SUBTITLE = "#8b95a3"
NODE_DISABLED_BODY = "#252a30"

#: 按插件 category 上色，让画布一眼能分出"这是界面操作还是文件操作"。
CATEGORY_COLORS: dict[str, str] = {
    "触发": "#d9534f",
    "时间": "#d9a441",
    "文件": "#5aa469",
    "网络": "#4a7fbf",
    "界面": "#a06cc4",
    "数据": "#3fa8a8",
    "系统": "#c2703d",
    "通用": "#6b7684",
}
DEFAULT_CATEGORY_COLOR = "#6b7684"

#: 端口颜色 = 数据类型。
TYPE_COLORS: dict[str, str] = {
    "any": "#9aa4b2",
    "string": "#5aa469",
    "text": "#5aa469",
    "code": "#3f8f56",
    "number": "#4a90d9",
    "integer": "#4a90d9",
    "bool": "#d98b4a",
    "enum": "#8e7cc3",
    "element": "#d96a9c",
    "file": "#c9a227",
    "folder": "#c9a227",
    "credential": "#c25b5b",
    "color": "#c78ad1",
    "list": "#6fa8a8",
    "dict": "#6fa8a8",
}
DEFAULT_TYPE_COLOR = "#9aa4b2"

#: 执行流专用色，与所有数据类型区分开。
EXEC_COLOR = "#c8ced8"
EXEC_ERROR_COLOR = "#d9534f"

# 运行态
STATE_RUNNING = "#e0b64a"
STATE_SUCCESS = "#4fae63"
STATE_FAILED = "#d9534f"
STATE_SKIPPED = "#6b7684"
STATE_IDLE = ""
#: 需要提醒但不算错误的情况（比如"有 3 条流程在用这个模块"）。
STATE_WARNING = "#e0b64a"

#: 卡片底部那行按钮的统一度量。
#: **「我的流程」和「我的模块」两个页面共用这一份。** 分开各写一个高度，结果是流程卡片
#: 按钮 32px、模块方砖 22px，字号也不一样 —— 同一类东西在两个页面长得不同，用户一眼就
#: 看得出来。度量放这儿，谁想改都得改同一个数。
ROW_BUTTON_HEIGHT = 32
ROW_BUTTON_SPACING = 6

STATE_COLORS = {
    "running": STATE_RUNNING,
    "success": STATE_SUCCESS,
    "failed": STATE_FAILED,
    "skipped": STATE_SKIPPED,
}

# 面板
PANEL_BG = "#242930"
PANEL_BORDER = "#333a44"
LOG_LEVEL_COLORS = {
    "debug": "#7f8a99",
    "info": "#c8ced8",
    "warning": "#e0b64a",
    "error": "#e06c6c",
}


def qcolor(hex_value: str, alpha: int = 255) -> QColor:
    color = QColor(hex_value)
    if alpha != 255:
        color.setAlpha(alpha)
    return color


def category_color(category: str) -> str:
    return CATEGORY_COLORS.get(category, DEFAULT_CATEGORY_COLOR)


def type_color(kind: str) -> str:
    return TYPE_COLORS.get(kind, DEFAULT_TYPE_COLOR)


def port_color(kind: str, data_type: str = "any") -> str:
    """端口颜色：执行流统一用灰白，数据端口按类型上色。"""
    if kind == "exec":
        return EXEC_COLOR
    return type_color(data_type)


# 节点与端口在 QGraphicsScene 里的 z 值
Z_EDGE = -1.0
Z_NODE = 1.0
Z_EDGE_PREVIEW = 5.0
Z_PORT = 2.0


# --------------------------------------------------------------------------- #
# 应用主题
# --------------------------------------------------------------------------- #

_STYLESHEET = f"""
QMainWindow, QWidget {{ background: {PANEL_BG}; color: {NODE_TEXT}; }}
/* 标签自己不该画背景 —— 上面那条 QWidget 规则会把它们也管上，
   于是卡片里的文字会多出一条比卡片底色略不同的色带。 */
QLabel {{ background: transparent; }}
QMenuBar {{ background: {PANEL_BG}; }}
QMenuBar::item:selected {{ background: {NODE_BORDER}; }}
QMenu {{ background: {PANEL_BG}; border: 1px solid {PANEL_BORDER}; }}
QMenu::item:selected {{ background: {NODE_BORDER_SELECTED}; }}

QToolBar {{ background: #262c34; border: none; spacing: 3px; padding: 5px; }}
QToolButton {{ padding: 4px 10px; border-radius: 4px; }}
QToolButton:hover {{ background: {NODE_BORDER}; }}
QToolButton:disabled {{ color: #5c6674; }}

QDockWidget {{ titlebar-close-icon: none; titlebar-normal-icon: none; }}
QDockWidget::title {{ background: #262c34; padding: 6px 8px; border-bottom: 1px solid {PANEL_BORDER}; }}

QTabWidget::pane {{ border: 1px solid {PANEL_BORDER}; }}
QTabBar::tab {{ background: #262c34; padding: 5px 14px; border: none; }}
QTabBar::tab:selected {{ background: {NODE_BORDER}; color: {NODE_TEXT}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: #1b1f25; border: 1px solid {NODE_BORDER}; border-radius: 4px; padding: 3px 6px;
    selection-background-color: {NODE_BORDER_SELECTED};
}}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {NODE_BORDER_SELECTED}; }}
QComboBox::drop-down {{ border: none; width: 16px; }}

QPushButton {{ background: {NODE_BORDER}; border: none; border-radius: 4px; padding: 5px 12px; }}
QPushButton:hover {{ background: #4a5462; }}
QPushButton:disabled {{ background: #2a3038; color: #5c6674; }}

QTreeWidget, QTableWidget, QListView {{ background: #1b1f25; border: none; outline: none; }}
QTreeWidget::item {{ padding: 3px 2px; }}
QTreeWidget::item:selected, QTableWidget::item:selected {{ background: {NODE_BORDER_SELECTED}; }}
QHeaderView::section {{ background: #262c34; border: none; padding: 5px; }}
QTableWidget {{ gridline-color: {PANEL_BORDER}; }}

QScrollBar:vertical {{ background: {PANEL_BG}; width: 11px; margin: 0; }}
QScrollBar:horizontal {{ background: {PANEL_BG}; height: 11px; margin: 0; }}
QScrollBar::handle {{ background: #454e5c; border-radius: 5px; min-height: 26px; }}
QScrollBar::handle:hover {{ background: #566072; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QStatusBar {{ background: #262c34; }}
QStatusBar::item {{ border: none; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 3px; border: 1px solid {NODE_BORDER}; background: #1b1f25; }}
QCheckBox::indicator:checked {{ background: {NODE_BORDER_SELECTED}; }}
QSplitter::handle {{ background: {PANEL_BORDER}; }}

/* ---- 自定义标题栏：菜单直接坐在最顶上那一行 ---- */
#titleBar {{ background: #1f242b; }}
#titleBar QMenuBar {{ background: transparent; }}
#titleBar QMenuBar::item {{ background: transparent; padding: 5px 11px; border-radius: 4px; }}
#titleBar QMenuBar::item:selected {{ background: {NODE_BORDER}; }}
QToolButton#windowButton {{ background: transparent; border: none; color: {NODE_TEXT}; font-size: 13px; }}
QToolButton#windowButton:hover {{ background: {NODE_BORDER}; }}
QToolButton#closeButton {{ background: transparent; border: none; color: {NODE_TEXT}; font-size: 13px; }}
QToolButton#closeButton:hover {{ background: #c0392b; color: #ffffff; }}

/* ---- 左侧导航栏 ---- */
#sideBar {{ background: #1f242b; border-right: 1px solid {PANEL_BORDER}; }}
#sideBarSection {{ color: #6b7684; font-size: 11px; padding: 10px 12px 3px 12px; }}
QPushButton#sideBarButton {{
    text-align: left; padding: 6px 10px; margin: 1px 6px;
    border: none; border-left: 3px solid transparent; border-radius: 5px;
    background: transparent; color: {NODE_TEXT};
}}
QPushButton#sideBarButton:hover {{ background: {NODE_BODY_SELECTED}; }}
QPushButton#sideBarButton:checked {{
    background: #2f3843; border-left: 3px solid {NODE_BORDER_SELECTED};
    color: {NODE_TEXT}; font-weight: bold;
}}
QPushButton#sideBarButton:disabled {{ color: #5c6674; }}
#sideBarFooter {{ color: #6b7684; font-size: 11px; padding: 8px 12px; }}

/* ---- 流程列表（表格视图） ---- */
QTableWidget#workflowTable {{ background: {BACKGROUND}; border: 1px solid {NODE_BORDER}; border-radius: 8px; }}
QTableWidget#workflowTable::item {{ padding: 6px 8px; }}
QTableWidget#workflowTable::item:selected {{ background: #2f3843; }}

/* ---- AI 对话 ---- */
QFrame#aiBubble {{ background: {NODE_BODY}; border: 1px solid {NODE_BORDER}; border-radius: 10px; }}
QFrame#aiBubbleUser {{ background: #2c4257; border: 1px solid #3c5a75; border-radius: 10px; }}
QFrame#aiDraftCard {{ background: #23282f; border: 1px solid {NODE_BORDER_SELECTED}; border-radius: 8px; }}

/* ---- 画布内的浮层提示（连线反馈等） ---- */
QLabel#canvasToast {{
    background: rgba(28, 33, 40, 235);
    color: {NODE_TEXT};
    border: 1px solid {NODE_BORDER_SELECTED};
    border-radius: 6px;
    padding: 6px 14px;
}}
"""


#: 界面字体候选，按优先级。前三项保证中文不会退化成方框。
_UI_FONT_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Segoe UI",
    "Noto Sans CJK SC",
    "PingFang SC",
)


def _pick_ui_font() -> str | None:
    available = set(QFontDatabase.families())
    for candidate in _UI_FONT_CANDIDATES:
        if candidate in available:
            return candidate
    return None


def apply_theme(app: Any) -> None:
    """深色主题。画布是深色的，如果面板还是系统浅色，整体会很割裂。"""
    app.setStyle("Fusion")

    family = _pick_ui_font()
    if family:
        font = app.font()
        font.setFamily(family)
        font.setPointSizeF(9.5)
        app.setFont(font)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(PANEL_BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(NODE_TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor("#1b1f25"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BACKGROUND))
    palette.setColor(QPalette.ColorRole.Text, QColor(NODE_TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(NODE_BORDER))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(NODE_TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(NODE_BORDER_SELECTED))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#2b3138"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(NODE_TEXT))
    app.setPalette(palette)
    app.setStyleSheet(_STYLESHEET)


# --------------------------------------------------------------------------- #
# 深色 / 浅色
# --------------------------------------------------------------------------- #

#: 导入时按深色算好的那份 QSS。切主题时拿它当替换源 —— 重写那一百行样式，收益只有
#: "看起来更干净"，风险是把已经调好的东西改坏。
_STYLESHEET_DARK = _STYLESHEET

#: 结构色映射：深 -> 浅。
#: **只映射结构色**（背景 / 边框 / 灰阶文字）。成功绿、失败红、分类色这些语义色一律
#: 不动 —— 它们表达的是含义，换个主题也该是同一个含义。
_LIGHT_MAP = {
    "#1e2228": "#f4f5f7",  # 画布背景
    "#272c34": "#e9ebee",  # 网格细线
    "#2f3540": "#dbdfe5",  # 网格粗线
    "#1b1f25": "#ffffff",  # 输入框底色
    "#2b3138": "#ffffff",  # 卡片 / 节点背景
    "#333b45": "#eaeef4",  # 选中的背景
    "#252a30": "#f0f1f3",  # 禁用的背景
    "#3d4550": "#c8ced7",  # 边框
    "#242930": "#f2f3f5",  # 面板背景
    "#262c34": "#e7eaee",  # 未选中的页签底色
    "#333a44": "#d6dae1",  # 面板边框
    "#2f3843": "#dde4ee",  # 侧边栏选中（QSS 里硬编码的）
    "#23282f": "#f7f8fa",  # AI 草稿卡片
    "#2c4257": "#dbe8f6",  # 用户气泡
    "#3c5a75": "#b8cfe6",  # 用户气泡边框
    "#dfe4ea": "#1d2230",  # 正文
    "#8b95a3": "#5b6675",  # 次要文字
    "#9aa4b2": "#5b6675",
    "#c8ced8": "#4a5563",  # 执行线
    "#6b7684": "#7a8492",
    "#5c6674": "#7a8492",
}

_MODE = "dark"

#: 深色原值,**在模块加载时就固定下来**（按名字索引）。
#: 别改成惰性收集 —— 第一次 set_mode("light") 之后常量已经是浅色的了，那时再收集
#: 就等于把浅色当成"原值"，再也切不回深色。这个坑我踩过。
#:
#: ``not name.startswith("_")`` 这条不能省：``"_DARK_HEX".isupper()`` 是 **True**
#: （下划线不算大小写，字母全大写就算 upper）。少了它，下面两张表会把自己也收进去，
#: 于是切浅色时 ``globals()["_DARK_HEX"] = {...}`` 把"深色原值"覆盖成浅色值，
#: 之后再也切不回深色。表里装自己 —— 踩过。
_DARK_HEX: dict[str, str] = {
    name: value
    for name, value in list(globals().items())
    if not name.startswith("_") and name.isupper()
    and isinstance(value, str)
    and value.startswith("#")
}
_DARK_DICTS: dict[str, dict[str, Any]] = {
    name: dict(value)
    for name, value in list(globals().items())
    if not name.startswith("_") and name.isupper() and isinstance(value, dict)
}


def _swap(value: Any, light: bool) -> Any:
    """把**值**换成对应主题的值。按值查表 —— 表和常量表是两张不同的表。"""
    if not isinstance(value, str) or not value.startswith("#"):
        return value
    if light:
        return _LIGHT_MAP.get(value, value)
    return _DARK_HEX.get(value, value)


def _map_text(text: str, light: bool) -> str:
    if not light:
        return text
    for dark_hex, light_hex in _LIGHT_MAP.items():
        text = text.replace(dark_hex, light_hex)
    return text


#: 各页面里那些模块级样式常量的**深色原文**，(模块名, 属性名) -> 原始字符串。
#: 必须存原文：常量改过之后再去读它，读到的已经是换算过的值，来回切几次就会糊掉。
_STYLE_SNAPSHOT: dict[tuple[str, str], str] = {}


def _patch_module_styles(light: bool) -> None:
    """把各页面里模块级的样式常量按当前主题改写一遍。

    这些常量是**导入时**算好的 f-string，里面冻的是深色值 —— 重建页面也没用，重建出来的
    还是那份老字符串。与其在十几处 ``setStyleSheet`` 上各包一层（容易漏，而且以后新加的
    地方又会忘），不如在源头把常量本身改掉：调用点一行都不用动。

    只认 ``末尾是 _STYLE 且含 #`` 的模块级字符串，范围足够窄，不会误伤别的东西。
    """
    import sys  # noqa: PLC0415 - 只有切主题时才需要

    for module_name, module in list(sys.modules.items()):
        if module is None or not module_name.startswith("host"):
            continue
        for attr, value in list(vars(module).items()):
            if not (attr.endswith("_STYLE") and isinstance(value, str) and "#" in value):
                continue
            key = (module_name, attr)
            if key not in _STYLE_SNAPSHOT:
                _STYLE_SNAPSHOT[key] = value
            setattr(module, attr, _map_text(_STYLE_SNAPSHOT[key], light))


def set_mode(mode: str) -> None:
    """切换主题。**必须在 apply_theme() 之前调用** —— 那份 QSS 是这时才算好的。"""
    global _MODE, _STYLESHEET

    light = mode == "light"
    _MODE = "light" if light else "dark"

    for name, dark in _DARK_HEX.items():
        globals()[name] = _LIGHT_MAP.get(dark, dark) if light else dark
    for name, dark in _DARK_DICTS.items():
        globals()[name] = {
            key: (_LIGHT_MAP.get(value, value) if light else value)
            for key, value in dark.items()
        }

    _STYLESHEET = _map_text(_STYLESHEET_DARK, light)
    _patch_module_styles(light)


def mode() -> str:
    return _MODE
