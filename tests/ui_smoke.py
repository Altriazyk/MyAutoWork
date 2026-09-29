"""界面冒烟测试（离屏渲染）。

    python tests/ui_smoke.py

用 ``QT_QPA_PLATFORM=offscreen`` 在无显示器环境下跑真实的 Qt 控件树，验证：

1. **首页是流程列表**：卡片由磁盘上的工作流文件生成，坏文件也能显示出来
2. **两种展示方式**：卡片 ⇄ 列表（表格）切换，数据一致
3. 左侧导航栏：流程 / 插件切换、新建/打开/刷新入口、展示方式切换
4. **点击即执行**：在首页点运行就能真跑起来，卡片/表格显示运行中与上次结果
5. 编辑器是二级页面：进编辑器才出现模块/属性面板，侧边栏收起
6. schema → 控件真的生效（File 长出浏览按钮、Element 长出瞄准镜、Text 是多行框）
7. 连线规则真的会拦人；属性面板 ↔ 画布 双向同步
8. 保存/读取往返不丢东西

顺便把界面截图写到 docs/ 下，方便直接看效果。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

# 必须在 import PySide6 之前设置，否则会尝试连接真实显示服务。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# offscreen 平台用的是"基础字体库"，它只扫描 QT_QPA_FONTDIR 指向的目录。
# 不告诉它系统字体在哪，界面上所有文字都会渲染成方框（截图也就没法看了）。
if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and "QT_QPA_FONTDIR" not in os.environ:
    _system_fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    if os.path.isdir(_system_fonts):
        os.environ["QT_QPA_FONTDIR"] = _system_fonts

from pathlib import Path  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QStatusBar,
)

from host import theme  # noqa: E402
from host.ai_panel import AiMessage, AiPanel, StubBackend  # noqa: E402
from host.constants import DIR_IN, DIR_OUT, VIEW_CARD, VIEW_LIST  # noqa: E402
from host.custom_view import workflows_using  # noqa: E402
from host.forms.builder import create_value_editor  # noqa: E402
from host.main_window import (  # noqa: E402
    MODE_CHECKLIST,
    MODE_CHECKLIST_EDIT,
    MODE_CUSTOM,
    MODE_EDITOR,
    MODE_MODULE,
    MODE_PLUGINS,
    MODE_WORKFLOWS,
    MainWindow,
)
from host.ai_settings import AiSettings  # noqa: E402
from host.plugin_view import PluginCard, PluginTile  # noqa: E402
from kernel.authoring import PluginDraft, plugin_template  # noqa: E402
from kernel.errors import MyAutoWorkError  # noqa: E402
from kernel.graph import (  # noqa: E402
    MODE_EXPR,
    MODE_LITERAL,
    ParamValue,
    load_workflow,
    save_workflow,
)
from host.canvas.node_item import NodeItem  # noqa: E402
from host.canvas.scene import WorkflowScene  # noqa: E402
from host.panels.inspector import InspectorPanel  # noqa: E402
from kernel.registry import Registry  # noqa: E402
from kernel.store import RunStore  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except (AttributeError, ValueError):  # pragma: no cover
    pass


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))

    def section(self, title: str) -> None:
        print(f"\n{title}")


def pump(app: QApplication, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def wait_for_run(app: QApplication, window: MainWindow, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while window._run_thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    pump(app, 0.25)


def build_fixtures(root: Path) -> dict[str, Path]:
    """在临时目录里准备一个流程库：正常的、带描述的、坏掉的。"""
    workflows = root / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)

    demo = workflows / "demo.json"
    # **自己造一个，不去复制仓库里的那份。**
    #
    # 原来这里是 shutil.copy2(REPO_ROOT/"workflows"/"demo.json") —— 而那个文件用户
    # 可以在界面上点「删除」删掉（删除走回收站，是正常用法）。删完之后测试崩在
    # FileNotFoundError，报的还跟被测代码看不出关系。测试要用的东西就自己造。
    #
    # 内容必须和原来那条一致：下面好几处断言认它的形状（4 个节点 4 条连线、n3 的端口、
    # 产出 out/demo_*.txt、工作流变量 greeting）。它同时把**三种参数来源**各用了一遍
    # ——常量（n1.note）、表达式（n2.reason / n3.path）、上游连线（n2→n3 的 content）。
    demo.write_text(
        json.dumps(
            {
                "id": "demo",
                "name": "演示：等待后写文件",
                "version": 1,
                "description": "手动触发 → 等待 → 写入文件 → 追加一行汇总。三种参数来源各跑一遍。",
                "variables": {"greeting": "你好，myautowork"},
                "nodes": [
                    {
                        "id": "n1",
                        "plugin": "core.trigger.manual",
                        "action": "start",
                        "pos": [60, 240],
                        "params": {"note": "命令行验收"},
                    },
                    {
                        "id": "n2",
                        "plugin": "core.time.delay",
                        "action": "sleep",
                        "pos": [340, 240],
                        "params": {
                            "seconds": 1.5,
                            "reason": {
                                "mode": "expr",
                                "value": "{{ $greeting }} —— 等界面加载",
                            },
                        },
                    },
                    {
                        "id": "n3",
                        "plugin": "core.file.write",
                        "action": "write",
                        "pos": [640, 110],
                        "params": {
                            "path": {"mode": "expr", "value": "out/demo_{{$date}}.txt"},
                            "content": {"mode": "upstream"},
                            "encoding": "utf-8",
                        },
                    },
                    {
                        "id": "n4",
                        "plugin": "core.file.write",
                        "action": "write",
                        "pos": [920, 340],
                        "params": {
                            "path": {"mode": "expr", "value": "out/demo_{{$date}}.txt"},
                            "content": {
                                "mode": "expr",
                                "value": "\n[{{ $time }}] 等待 {{ $node.n2.slept }} 秒完成；"
                                "备注：{{ $greeting }}",
                            },
                            "append": True,
                        },
                    },
                ],
                "edges": [
                    {"from": "n1", "to": "n2", "kind": "exec"},
                    {"from": "n2", "to": "n3", "kind": "exec"},
                    {
                        "from": "n2",
                        "from_port": "finished_at",
                        "to": "n3",
                        "to_port": "content",
                        "kind": "data",
                    },
                    {"from": "n3", "to": "n4", "kind": "exec"},
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    second = workflows / "report.json"
    second.write_text(
        json.dumps(
            {
                "id": "report",
                "name": "每日报表导出",
                "description": "登录系统，导出昨天的报表到本地。",
                "nodes": [
                    {"id": "n1", "plugin": "core.trigger.manual", "action": "start", "pos": [60, 200]},
                    {
                        "id": "n2",
                        "plugin": "core.time.delay",
                        "action": "sleep",
                        "pos": [340, 200],
                        "params": {"seconds": 0.2},
                    },
                ],
                "edges": [{"from": "n1", "to": "n2", "kind": "exec"}],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    broken = workflows / "broken.json"
    broken.write_text("{ 这不是合法 JSON", encoding="utf-8")

    return {"demo": demo, "second": second, "broken": broken, "dir": workflows}


def card_texts(card: PluginCard) -> str:
    return " ".join(label.text() for label in card.findChildren(QLabel))


def main() -> int:
    checker = Checker()
    app = QApplication([sys.argv[0]])
    theme.apply_theme(app)

    tmpdir = Path(tempfile.mkdtemp(prefix="myautowork-ui-"))
    docs_dir = REPO_ROOT / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    fixtures = build_fixtures(tmpdir)

    store = RunStore(tmpdir / "history.db")
    # 守卫用：这个测试全程只该动临时目录。仓库的 plugins/ 曾经被写脏过一次
    # （某次 save() 用了默认标识 user.my_module），跑完要对一下。
    repo_plugins_before = {p.name for p in (REPO_ROOT / "plugins").iterdir()}
    # 插件目录拷一份到临时区：AI 创建模块的测试会往里面写东西，不能污染仓库。
    isolated_plugins = tmpdir / "plugins"
    # **只复制内置的那几个，不要整目录复制。** 用户的 plugins/ 里可能有自己写的模块，
    # 整目录拖过来的话，"我的模块页只有 1 条"这类断言会因为别人的东西而红 ——
    # 测试不该依赖机器上装了什么。
    isolated_plugins.mkdir()
    for builtin_id in (
        "core.app",
        "core.file.write",
        "core.flow",
        "core.time.delay",
        "core.trigger.manual",
        "win.input",
        "win.uia",
        "win.window",
    ):
        # 同样跳过 .profile —— 那是浏览器插件的登录数据，里面有被锁住的活文件。
        shutil.copytree(
            REPO_ROOT / "plugins" / builtin_id,
            isolated_plugins / builtin_id,
            ignore=shutil.ignore_patterns(".profile", "__pycache__", ".venv"),
        )
    # 造一个"用户自己的模块"：自定义模块页管的就是这些。
    from kernel.authoring import PluginDraft, plugin_template  # noqa: PLC0415

    PluginDraft(
        plugin_id="user.my_tool",
        name="我的小工具",
        description="用户自己加的模块",
        category="示例",
        code=plugin_template(
            plugin_id="user.my_tool", name="我的小工具", description="用户自己加的模块"
        ),
        provides={"actions": ["run"]},
        # 故意声明一个装不上的包 —— 界面上该长出「安装依赖」按钮。
        dependencies=["完全不存在的包xyz>=1.0"],
    ).write(isolated_plugins)
    registry = Registry(isolated_plugins, repo_root=REPO_ROOT)
    window = MainWindow(
        registry,
        store=store,
        workdir=tmpdir / "work",
        workflows_dir=fixtures["dir"],
        plugins_dir=isolated_plugins,
        # **必须显式给一份"没配过"的 AI 设置。** 不给的话 MainWindow 会去读机器上的
        # %APPDATA%\myautowork\ai.json —— 测试结果就取决于跑测试那台机器配没配过 AI。
        # 测试不该依赖机器上装了什么、配了什么。
        ai_settings=AiSettings(),
    )
    window.workerStderr
    registry.on_worker_stderr = window.workerStderr.emit
    window.confirm_on_close = False
    # 测试里不要"未保存"确认框：它是模态的，离屏环境下没人点，会直接卡死。
    window._confirm_discard = lambda: True  # type: ignore[method-assign]
    window.resize(1480, 920)
    window.show()
    app.processEvents()

    try:
        # -- 1. 外壳：无边框 + 自绘标题栏 + 侧边栏 ----------------------------
        checker.section("1) 窗口外壳")
        checker.check("无边框",
                      bool(window.windowFlags() & Qt.WindowType.FramelessWindowHint),
                      str(window.windowFlags()))
        checker.check("有自绘标题栏", window.title_bar is not None)
        checker.check("菜单挂在标题栏里（不是原生菜单栏）",
                      window.title_bar.menu_bar is window.menu_bar,
                      str(type(window.menu_bar).__name__))
        menu_titles = [a.text() for a in window.menu_bar.actions()]
        checker.check("标题栏里是「文件 编辑 视图 运行 工具 帮助」",
                      menu_titles == ["文件", "编辑", "视图", "运行", "工具", "帮助"], str(menu_titles))
        checker.check("标题栏没有显示软件名",
                      "myautowork" not in " ".join(
                          label.text() for label in window.title_bar.findChildren(QLabel)
                      ), "")
        checker.check("启动后停在流程列表", window.mode == MODE_WORKFLOWS, window.mode)
        checker.check("窗口标题不含软件名", "myautowork" not in window.windowTitle(),
                      window.windowTitle())
        # 回归守卫：QDockWidget 曾经把标题栏挤在中间，菜单只占一段、窗口按钮压在面板上。
        checker.check("标题栏横跨整个窗口宽度",
                      window.title_bar.width() >= window.width() - 4,
                      f"titlebar={window.title_bar.width()} window={window.width()}")
        checker.check("标题栏在最顶上（纵坐标为 0）",
                      window.title_bar.mapTo(window, window.title_bar.rect().topLeft()).y() == 0,
                      str(window.title_bar.mapTo(window, window.title_bar.rect().topLeft())))
        menu_map = {
            action.text(): [item.text() for item in action.menu().actions() if not item.isSeparator()]
            for action in window.menu_bar.actions()
            if action.menu() is not None
        }
        checker.check("帮助菜单里没有插件入口（插件从左侧导航栏进）",
                      menu_map.get("帮助") == ["关于"], str(menu_map.get("帮助")))
        checker.check("「刷新」在运行菜单里", "刷新" in menu_map.get("运行", []),
                      str(menu_map.get("运行")))
        checker.check("运行菜单是 刷新 / 停止（「运行当前流程」是编辑器的事，工具栏上有）",
                      menu_map.get("运行") == ["刷新", "停止"], str(menu_map.get("运行")))

        # **顶部菜单只放整个软件级别的功能。** 编辑器专有的那些（保存/另存为/返回列表/
        # 删除选中/缩放/运行当前流程）都在工具栏上，不该在菜单里再出现一次 —— 出现了就得
        # 在非编辑器状态下藏起来，而"藏来藏去"本身就是它们不该在这儿的信号。
        editor_only = {"← 流程列表", "保存", "另存为", "删除选中", "运行",
                       "放大", "缩小", "重置缩放", "适应窗口"}
        leaked = {
            name: [item for item in items if item in editor_only]
            for name, items in menu_map.items()
        }
        leaked = {name: items for name, items in leaked.items() if items}
        checker.check("菜单里没有编辑器专有的功能", leaked == {}, str(leaked))
        checker.check("但它们仍然在工具栏上（去掉菜单不影响用）",
                      all(action in window.toolbar.actions() for action in (
                          window.action_save, window.action_save_as, window.action_delete,
                          window.action_zoom_in, window.action_zoom_out,
                          window.action_zoom_reset, window.action_fit,
                      )),
                      str([a.text() for a in window.toolbar.actions()]))
        checker.check("快捷键也还在（跟出现在哪个菜单无关）",
                      window.action_save.shortcut().toString() != ""
                      and window.action_zoom_reset.shortcut().toString() != "",
                      f"{window.action_save.shortcut().toString()} / "
                      f"{window.action_zoom_reset.shortcut().toString()}")
        checker.check("左侧栏不再有刷新按钮", not hasattr(window.sidebar, "action_refresh"))
        checker.check("左侧栏不再有「打开文件」", not hasattr(window.sidebar, "action_open"))
        checker.check("左侧栏不再有「打开流程目录」", not hasattr(window.sidebar, "action_reveal"))
        checker.check("左侧栏「操作」段只剩新建流程",
                      hasattr(window.sidebar, "action_new")
                      and window.sidebar.action_new.text() == "新建流程")
        checker.check("侧边栏可见", window.sidebar.isVisible())
        checker.check("工具栏在首页隐藏（操作都在侧边栏）", not window.toolbar.isVisible())
        checker.check("模块面板在首页隐藏", not window.palette_panel.isVisible())
        checker.check("属性面板在首页隐藏", not window.inspector.isVisible())
        checker.check("运行日志在首页可见", window.log_panel.isVisible())
        # 底边栏现在有 AI 对话，初始高度翻了一倍（200 -> 400）。
        checker.check("底边栏初始高度约 200（不占太多地方）",
                      150 <= window.log_panel.height() <= 320,
                      str(window.log_panel.height()))
        # 底部那条常驻状态栏整个拿掉了：没事的时候它只是一行空白。
        # 连线反馈改走画布浮层，缩放比例印在工具栏按钮上。
        checker.check("窗口里已经没有状态栏", window.findChild(QStatusBar) is None)
        checker.check("缩放比例印在工具栏按钮上",
                      window.action_zoom_reset.text().endswith("%"),
                      window.action_zoom_reset.text())
        window.view.reset_zoom()
        pump(app, 0.05)
        checker.check("复位后按钮显示 100%",
                      window.action_zoom_reset.text() == "100%",
                      window.action_zoom_reset.text())
        window.view.hide_toast()
        window.scene.statusMessage.emit("连不上：类型不兼容")
        pump(app, 0.05)
        checker.check("连线反馈出现在画布浮层上（不再去底部）",
                      "连不上" in window.view.toast_text, window.view.toast_text)
        window.view.hide_toast()
        pump(app, 0.05)
        checker.check("浮层可以收起", window.view.toast_text == "")
        checker.check("状态栏不再重复显示插件数（左侧栏底部已有）",
                      not hasattr(window, "_plugin_label"))
        # 空页签如果不给提示，切过去看着和上一个页签一样，用户会以为"点了没反应"。
        # 注意用 isHidden() 而不是 isVisible()：非当前页签的父页被 QTabWidget 藏起来了，
        # isVisible() 恒为 False，测不出"我们要求它显示"这件事。
        checker.check("变量页为空时有提示", not window.log_panel.var_hint.isHidden())
        checker.check("历史页为空时有提示", not window.log_panel.history_hint.isHidden())
        checker.check("变量页提示写清了什么时候会有内容",
                      "set_var" in window.log_panel.var_hint.text(),
                      window.log_panel.var_hint.text())

        # -- 2. 首页：卡片视图 ------------------------------------------------
        checker.section("2) 首页：卡片视图")
        cards = window.workflow_list._cards
        checker.check("三份文件都出现在列表里（含坏掉的那份）", len(cards) == 3,
                      str(sorted(p.name for p in cards)))

        demo_card = window.workflow_list.card_for(fixtures["demo"])
        second_card = window.workflow_list.card_for(fixtures["second"])
        broken_card = window.workflow_list.card_for(fixtures["broken"])
        checker.check("demo 卡片存在", demo_card is not None)
        checker.check("坏文件也有卡片（不是整页挂掉）", broken_card is not None)

        if second_card is not None:
            checker.check("卡片显示流程名称", second_card.title.text() == "每日报表导出",
                          second_card.title.text())
            checker.check("卡片显示描述", "登录系统" in second_card.description.text(),
                          second_card.description.text())
            checker.check("卡片显示节点数", "2 个节点" in second_card.meta.text(), second_card.meta.text())
            checker.check("未跑过的显示「尚未运行过」", "尚未运行过" in second_card.meta.text(),
                          second_card.meta.text())
            checker.check("运行按钮可用", second_card.run_button.isEnabled())

        if broken_card is not None:
            checker.check("坏文件的运行按钮被禁用", not broken_card.run_button.isEnabled())
            checker.check("坏文件说明了原因", "无法解析" in broken_card.description.text(),
                          broken_card.description.text())

        checker.check("顶部统计显示条数", "共 3 条" in window.workflow_list.summary.text(),
                      window.workflow_list.summary.text())
        checker.check("运行菜单里的刷新能用", window.action_refresh.isEnabled())
        window.action_refresh.trigger()
        pump(app, 0.15)
        checker.check("触发刷新后列表还在", len(window.workflow_list._cards) == 3,
                      str(len(window.workflow_list._cards)))
        checker.check("统计里点出了坏文件数量",
                      "1 条无法解析" in window.workflow_list.summary.text(),
                      window.workflow_list.summary.text())
        checker.check("侧边栏底部只显示流程数（不再重复插件数）",
                      "3 条流程" in window.sidebar.footer.text()
                      and "插件" not in window.sidebar.footer.text(),
                      window.sidebar.footer.text())

        window.workflow_list.search.setText("报表")
        app.processEvents()
        checker.check("搜索能过滤", len(window.workflow_list._cards) == 1,
                      str(sorted(p.name for p in window.workflow_list._cards)))
        window.workflow_list.search.setText("")
        app.processEvents()
        checker.check("清空搜索后恢复", len(window.workflow_list._cards) == 3)

        # -- 2b. 网格排列 ------------------------------------------------------
        checker.section("2b) 卡片是网格排列（不是一行一块）")
        from host.flow_layout import FlowLayout

        checker.check("用的是流式布局",
                      isinstance(window.workflow_list.grid_layout, FlowLayout),
                      type(window.workflow_list.grid_layout).__name__)
        tiles = list(window.workflow_list._cards.values())
        checker.check("卡片是固定尺寸的方砖",
                      all(t.width() == tiles[0].width() and t.height() == tiles[0].height()
                          for t in tiles),
                      str([(t.width(), t.height()) for t in tiles]))
        first_row_y = {t.y() for t in tiles}
        checker.check("窗口够宽时三块排在同一行", len(first_row_y) == 1, str(sorted(first_row_y)))
        xs = sorted(t.x() for t in tiles)
        checker.check("同一行里从左到右依次摆放", xs[0] < xs[1] < xs[2], str(xs))

        # 拉窄窗口应该自动换行，而且不该把面板挤没
        window.resize(880, 900)
        pump(app, 0.4)
        wrapped_y = {t.y() for t in window.workflow_list._cards.values()}
        checker.check("窗口变窄后卡片换行", len(wrapped_y) >= 2, str(sorted(wrapped_y)))
        checker.check("拉窄不会把侧边栏挤没",
                      window.sidebar.isVisible() and window.sidebar.width() > 100,
                      f"visible={window.sidebar.isVisible()} width={window.sidebar.width()}")
        checker.check("拉窄不会把日志面板挤没",
                      window.log_panel.isVisible() and window.log_panel.height() > 50,
                      f"visible={window.log_panel.isVisible()} height={window.log_panel.height()}")

        window.resize(1480, 920)
        pump(app, 0.4)
        checker.check("拉回宽度后又回到一行",
                      len({t.y() for t in window.workflow_list._cards.values()}) == 1)
        checker.check("拉回后侧边栏还在", window.sidebar.width() > 100,
                      str(window.sidebar.width()))
        checker.check("拉回后日志面板还在", window.log_panel.height() > 50,
                      str(window.log_panel.height()))

        shot_home = docs_dir / "screenshot-home.png"
        checker.check("卡片视图截图已写出", window.grab().save(str(shot_home)), str(shot_home))

        # -- 3. 切换成列表（通栏卡片）视图 ------------------------------------
        checker.section("3) 展示方式：网格 ⇄ 通栏列表")
        checker.check("左侧栏里已经没有展示方式", not hasattr(window.sidebar, "view_card"))
        checker.check("卡片/列表**不在**「视图」菜单里（挪到各页右上角了）",
                      window.action_list_view not in window.view_menu.actions()
                      and window.action_card_view not in window.view_menu.actions())
        checker.check("但快捷键还留着",
                      window.action_card_view.shortcut().toString() == "Ctrl+1"
                      and window.action_list_view.shortcut().toString() == "Ctrl+2")
        checker.check("流程页右上角有自己的切换按钮",
                      window.workflow_list.toggle is not None)
        checker.check("插件页也有自己的", window.plugin_view.toggle is not None)
        checker.check("我的模块页也有自己的", window.custom_view.toggle is not None)
        checker.check("菜单文案说明了排法",
                      "网格" in window.action_card_view.text() and "通栏" in window.action_list_view.text(),
                      f"{window.action_card_view.text()} / {window.action_list_view.text()}")

        window.action_list_view.trigger()
        pump(app, 0.3)
        checker.check("切到列表视图", window.workflow_list.view_mode == VIEW_LIST,
                      window.workflow_list.view_mode)
        checker.check("菜单勾选状态同步", window.action_list_view.isChecked())
        checker.check("表格已经去掉了", not hasattr(window.workflow_list, "table"))

        rows = window.workflow_list._rows
        checker.check("三条流程各占一行", len(rows) == 3, str(sorted(p.name for p in rows)))
        row_widgets = list(rows.values())
        checker.check("一行一条（纵向依次排列）", len({r.y() for r in row_widgets}) == 3,
                      str(sorted(r.y() for r in row_widgets)))
        checker.check("每行占满宽度", all(r.width() > 600 for r in row_widgets),
                      str([r.width() for r in row_widgets]))
        broken_row = window.workflow_list.row_for(fixtures["broken"])
        checker.check("坏文件那一行写明了原因",
                      broken_row is not None and "无法解析" in broken_row.description.text(),
                      broken_row.description.text() if broken_row else "")
        checker.check("坏文件那一行的运行按钮被禁用",
                      broken_row is not None and not broken_row.run_button.isEnabled())

        shot_list = docs_dir / "screenshot-home-list.png"
        checker.check("列表视图截图已写出", window.grab().save(str(shot_list)), str(shot_list))

        window.action_card_view.trigger()
        pump(app, 0.15)
        checker.check("能切回网格视图", window.workflow_list.view_mode == VIEW_CARD)

        # -- 4. 在首页点击运行 ------------------------------------------------
        checker.section("4) 在首页点击运行")
        demo_card = window.workflow_list.card_for(fixtures["demo"])
        second_card = window.workflow_list.card_for(fixtures["second"])
        assert demo_card is not None
        demo_card.run_button.click()
        pump(app, 0.1)
        checker.check("运行线程已启动", window._run_thread is not None)
        checker.check("首页记住了正在跑哪一条",
                      window.workflow_list.running_path == fixtures["demo"],
                      str(window.workflow_list.running_path))
        checker.check("运行按钮变成禁用（防重复触发）", not window.action_run.isEnabled())
        checker.check("停止按钮可用", window.action_stop.isEnabled(),
                      f"stop={window.action_stop.isEnabled()} visible={window.action_stop.isVisible()}")
        checker.check("其它卡片的运行按钮也被禁用",
                      second_card is not None and not second_card.run_button.isEnabled())

        wait_for_run(app, window)
        checker.check("运行线程已结束", window._run_thread is None)
        checker.check("首页清掉了运行中标记", window.workflow_list.running_path is None)

        log_text = window.log_panel.log_view.toPlainText()
        checker.check("日志里有运行结束结论", "运行结束" in log_text, log_text[-300:])
        checker.check("日志标注了来源是流程列表", "流程列表" in log_text, log_text[-600:])

        refreshed = window.workflow_list.card_for(fixtures["demo"])
        checker.check("卡片更新为「上次运行：成功」",
                      refreshed is not None and "上次运行：成功" in refreshed.meta.text(),
                      refreshed.meta.text() if refreshed else "")
        checker.check("运行历史里有记录", window.log_panel.history_table.rowCount() >= 1)
        checker.check("变量面板拿到了工作流变量", window.log_panel.var_table.rowCount() >= 1)
        checker.check("变量页的空状态提示消失了", window.log_panel.var_hint.isHidden())
        checker.check("历史页的空状态提示消失了", window.log_panel.history_hint.isHidden())
        checker.check("工作流真的产出了文件",
                      len(list((tmpdir / "work").glob("out/demo_*.txt"))) == 1)

        # 列表视图里也要能看到新结果
        window.action_list_view.trigger()
        pump(app, 0.3)
        demo_row = window.workflow_list.row_for(fixtures["demo"])
        checker.check("通栏列表里的「上次运行」也更新了",
                      demo_row is not None and "上次运行：成功" in demo_row.meta.text(),
                      demo_row.meta.text() if demo_row else "")
        window.action_card_view.trigger()
        pump(app, 0.15)

        shot_run = docs_dir / "screenshot-run.png"
        checker.check("运行态截图已写出", window.grab().save(str(shot_run)), str(shot_run))

        # -- 5. 插件页 --------------------------------------------------------
        checker.section("5) 侧边栏 → 插件页")
        window.sidebar.nav_plugins.click()
        pump(app, 0.2)
        checker.check("切到了插件页", window.mode == MODE_PLUGINS, window.mode)
        checker.check("侧边栏导航状态同步", window.sidebar.nav_plugins.isChecked())
        # 插件页**只列内置的** —— 自己的模块归「我的模块」页。
        expected_plugins = sum(
            1 for pid in window.registry.manifests if window.registry.is_builtin(pid)
        )
        plugin_cards = window.plugin_view.findChildren(PluginCard)
        checker.check("插件页只列内置插件",
                      window.plugin_view.plugin_count() == expected_plugins,
                      f"{window.plugin_view.plugin_count()} vs 内置 {expected_plugins}")
        checker.check("统计写的是「内置插件」",
                      f"共 {expected_plugins} 个插件" in window.plugin_view.summary.text(),
                      window.plugin_view.summary.text())
        checker.check("自己的模块不出现在这一页",
                      window.plugin_view.card_for("user.my_tool") is None
                      and all(
                          window.registry.is_builtin(str(p.get("id") or ""))
                          for p in window.plugin_view._plugins
                      ),
                      str([p.get("id") for p in window.plugin_view._plugins]))
        all_text = " ".join(card_texts(card) for card in plugin_cards)
        checker.check("能看到写文件动作", "写文件" in all_text)
        checker.check("能看到端口数量摘要", "5 入 / 4 出" in all_text, all_text[:200])
        checker.check("界面自动化的三个插件都在", "激活窗口" in all_text and "点击元素" in all_text
                      and "输入文本" in all_text, all_text[:300])

        shot_plugins = docs_dir / "screenshot-plugins.png"
        checker.check("插件页截图已写出", window.grab().save(str(shot_plugins)), str(shot_plugins))

        # -- 5b. 我的模块页 ----------------------------------------------------
        checker.section("5b) 自定义模块：看得见、改得动、删得掉")
        builtin_ids = {
            "win.uia", "win.input", "win.window",
            "core.file.write", "core.time.delay", "core.trigger.manual",
        }
        # 5b 的导航结构：一个「我的模块」导航项，模块本身列在页面里（不是铺在左侧栏）
        checker.check("导航第一项是「我的流程」",
                      hasattr(window.sidebar, "nav_workflows")
                      and window.sidebar.nav_workflows.text() == "我的流程")
        checker.check("导航里不再有「自动化流程」这个旧名字",
                      window.sidebar.nav_workflows.text() != "自动化流程")
        checker.check("页面标题也跟着改",
                      window.workflow_list.heading.text() == "我的流程",
                      window.workflow_list.heading.text())
        checker.check("导航里有「我的模块」",
                      hasattr(window.sidebar, "nav_custom")
                      and window.sidebar.nav_custom.text() == "我的模块")
        checker.check("导航里不再有「自定义模块」这个旧名字",
                      window.sidebar.nav_custom.text() != "自定义模块")
        checker.check("「新建模块」进了操作列",
                      hasattr(window.sidebar, "action_new_module")
                      and window.sidebar.action_new_module.text() == "新建模块")
        checker.check("操作列现在是 新建流程 / 新建模块",
                      [window.sidebar.action_new.text(),
                       window.sidebar.action_new_module.text()] == ["新建流程", "新建模块"])
        checker.check("左侧栏里不再铺模块行（模块列在页面上）",
                      not hasattr(window.sidebar, "_module_buttons")
                      and not hasattr(window.sidebar, "module_host"))

        window.sidebar.nav_custom.click()
        pump(app, 0.25)
        checker.check("点「我的模块」切到了这一页", window.mode == MODE_CUSTOM, window.mode)
        checker.check("窗口标题跟着变", "我的模块" in window.windowTitle(), window.windowTitle())
        checker.check("这个导航项是选中的", window.sidebar.nav_custom.isChecked())
        checker.check("其它导航项没跟着亮",
                      not window.sidebar.nav_workflows.isChecked()
                      and not window.sidebar.nav_plugins.isChecked())

        # 切走再切回来，选中态要跟着走（不会"一直选着"）
        window.sidebar.nav_workflows.click()
        pump(app, 0.2)
        checker.check("切到流程列表后「我的模块」松开",
                      not window.sidebar.nav_custom.isChecked())
        window.sidebar.nav_custom.click()
        pump(app, 0.2)
        checker.check("切回来又选中", window.sidebar.nav_custom.isChecked())

        custom_ids = set(window.custom_view._rows)
        checker.check("列出了用户自己的模块", "user.my_tool" in custom_ids, str(sorted(custom_ids)))
        checker.check("内置模块不出现在这一页（那里删不掉）",
                      not (custom_ids & builtin_ids), str(sorted(custom_ids)))
        checker.check("内置模块被正确识别",
                      all(window.registry.is_builtin(pid) for pid in builtin_ids))
        checker.check("自定义模块不会被误判成内置",
                      not window.registry.is_builtin("user.my_tool"))
        checker.check("插件页仍然列出全部插件（含内置）",
                      window.plugin_view.plugin_count() == expected_plugins)

        row = window.custom_view.row_for("user.my_tool")
        checker.check("模块卡片渲染出来了", row is not None)

        # -- 依赖：按钮在**每条卡片上**，不在页面上 --------------------------
        # 注意区分两件事：**装依赖**（某个模块缺包）在卡片上；**装插件包**（导入一个
        # zip）是整页的动作，在页头。前者针对具体某条模块，后者不针对任何一条。
        checker.check("插件页的页面级按钮是「安装插件包」，不是装依赖",
                      hasattr(window.plugin_view, "install_button")
                      and window.plugin_view.install_button.text() == "安装插件包…",
                      getattr(getattr(window.plugin_view, "install_button", None), "text", lambda: "无")())
        checker.check("我的模块页没有页面级的装依赖按钮",
                      not hasattr(window.custom_view, "install_button"))
        checker.check("缺依赖的模块卡片上有「安装依赖」",
                      row is not None and row.install_button is not None
                      and row.install_button.text() == "安装依赖",
                      str(getattr(row, "install_button", None)))
        checker.check("tooltip 里点名了缺哪个包",
                      row is not None and row.install_button is not None
                      and "完全不存在的包xyz" in row.install_button.toolTip(),
                      row.install_button.toolTip() if row and row.install_button else "")
        checker.check("不缺依赖的模块卡片上没有这个按钮",
                      window.custom_view.row_for("user.second") is None
                      or window.custom_view.row_for("user.second").install_button is None)
        tile = window.custom_view.tile_for("user.my_tool")
        checker.check("网格方砖上也有（两种排法一致）",
                      tile is not None and tile.install_button is not None)

        window.custom_view.set_install_enabled(False)
        pump(app, 0.05)
        checker.check("安装期间按钮被禁掉（同时开几个 pip 只会把环境搞乱）",
                      not row.install_button.isEnabled()
                      and row.install_button.text() == "正在安装…")
        window.custom_view.set_install_enabled(True)
        pump(app, 0.05)
        checker.check("装完恢复", row.install_button.isEnabled())
        checker.check("卡片上能看到它有几个动作",
                      row is not None and "1 个动作" in card_texts(row),
                      card_texts(row)[:120] if row else "")

        # 用它的流程数 —— 这是删除前唯一的判断依据
        probe_dir = tmpdir / "probe_workflows"
        probe_dir.mkdir()
        (probe_dir / "a.json").write_text(
            json.dumps({"id": "a", "name": "引用它的流程",
                        "nodes": [{"id": "n1", "plugin": "user.my_tool", "action": "run"}],
                        "edges": []}, ensure_ascii=False), encoding="utf-8")
        (probe_dir / "b.json").write_text(
            json.dumps({"id": "b", "name": "无关的流程",
                        "nodes": [{"id": "n1", "plugin": "core.time.delay", "action": "sleep"}],
                        "edges": []}, ensure_ascii=False), encoding="utf-8")
        (probe_dir / "broken.json").write_text("{ 不是 JSON", encoding="utf-8")

        used = workflows_using(probe_dir, "user.my_tool")
        checker.check("能算出哪些流程在用这个模块", used == ["引用它的流程"], str(used))
        checker.check("无关的流程不算进来", "无关的流程" not in used)
        checker.check("坏掉的流程文件不会让统计崩掉", workflows_using(probe_dir, "core.time.delay") == ["无关的流程"])

        shot_custom = docs_dir / "screenshot-custom.png"
        checker.check("自定义模块页截图已写出", window.grab().save(str(shot_custom)), str(shot_custom))

        # 删除。确认框在离屏下会挂住，直接放行。
        target_dir = isolated_plugins / "user.my_tool"
        checker.check("删除前文件夹还在", target_dir.is_dir())
        checker.check("内置模块没有删除按钮",
                      window.custom_view.row_for("win.uia") is None)

        original_question = QMessageBox.question
        QMessageBox.question = staticmethod(  # type: ignore[assignment]
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes
        )
        try:
            row.delete_button.click()
            pump(app, 0.5)
        finally:
            QMessageBox.question = original_question  # type: ignore[assignment]

        checker.check("文件夹真的被删掉了", not target_dir.exists(), str(target_dir))
        checker.check("注册表里也没有了", not window.registry.has("user.my_tool"))
        checker.check("页面上也消失了", window.custom_view.row_for("user.my_tool") is None)
        checker.check("删掉自己的模块不影响插件页（那一页本来就不列它）",
                      window.plugin_view.plugin_count() == expected_plugins,
                      str(window.plugin_view.plugin_count()))
        checker.check("日志里记了一笔（并且说明是删到回收站，不是永久删除）",
                      "已删除插件 user.my_tool" in window.log_panel.log_view.toPlainText()
                      and "回收站" in window.log_panel.log_view.toPlainText())

        checker.check("删完之后页面显示空状态",
                      any("还没有自定义模块" in label.text()
                          for label in window.custom_view.findChildren(QLabel)),
                      "没找到空状态提示")
        checker.check("页面上也空了", not window.custom_view._rows,
                      str(sorted(window.custom_view._rows)))

        # -- 删：流程卡片和插件卡片上的删除按钮 --------------------------------
        checker.check("导航里是「我的插件」",
                      window.sidebar.nav_plugins.text() == "我的插件")
        checker.check("插件页大标题也是「我的插件」",
                      "我的插件" in [label.text() for label in window.plugin_view.findChildren(QLabel)])
        uia_card = window.plugin_view.card_for("win.uia")
        checker.check("插件卡片上有删除按钮",
                      uia_card is not None and hasattr(uia_card, "delete_button"))
        plugin_tiles = window.plugin_view.findChildren(PluginTile)
        checker.check("插件方砖上也有",
                      bool(plugin_tiles)
                      and all(hasattr(tile, "delete_button") for tile in plugin_tiles),
                      f"{len(plugin_tiles)} 块方砖")

        # 内置插件默认**仍然拒绝**删除 —— 保护留在内核那一层，不靠调用方自觉
        try:
            window.registry.remove("win.uia")
        except Exception as exc:
            checker.check("内置插件默认仍然拒绝删除（force 才放开）",
                          "内置" in str(exc), str(exc))
        else:
            checker.check("内置插件默认仍然拒绝删除（force 才放开）", False, "居然删成功了")

        first_entry = window.library.entries[0]
        checker.check("流程卡片上也有删除按钮",
                      window.workflow_list.card_for(first_entry.path) is not None
                      and hasattr(window.workflow_list.card_for(first_entry.path),
                                  "delete_button"))

        # 真的删一个流程：造一个临时文件走一遍
        temp_flow = window.workflows_dir / "待删除的流程.json"
        temp_flow.write_text(
            json.dumps({"id": "tmp.delete", "name": "待删除的流程", "nodes": [], "edges": []},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        window.library.discover()
        window.library.remove(temp_flow)
        checker.check("删流程走的是真的删除（文件不在了）", not temp_flow.exists())
        checker.check("删完立刻从列表里摘掉（不留点不动的幽灵）",
                      all(Path(e.path) != temp_flow for e in window.library.entries))

        # **路径越界**：宁可窄。删除这种操作判据放宽的代价是删掉用户别的东西
        try:
            window.library.remove(REPO_ROOT / "README.md")
        except Exception as exc:
            checker.check("拒绝删除流程目录以外的文件", "拒绝" in str(exc), str(exc))
        else:
            checker.check("拒绝删除流程目录以外的文件", False, "居然去删仓库里的文件了！")
        checker.check("那个文件当然还在", (REPO_ROOT / "README.md").is_file())

        # 网格方砖的「位置」发出去的必须是**目录路径**，不是模块标识。
        # 它跟着另外三个按钮共用过一个循环，一起发 plugin_id —— 点下去只会得到一句
        # "目录不存在：fgo"。列表行那边一直是对的，所以只有网格视图坏，很容易漏掉。
        from host.custom_view import ModuleTile  # noqa: PLC0415

        probe_tile = ModuleTile(
            {
                "id": "demo.probe",
                "path": r"C:\tmp\demo.probe",
                "name": "探针",
                "version": "0.1.0",
                "description": "",
                "category": "示例",
                "actions": [],
                "triggers": [],
            },
            [],
        )
        revealed: list[str] = []
        probe_tile.revealRequested.connect(revealed.append)
        for probe_button in probe_tile.findChildren(QPushButton):
            if probe_button.text() == "位置":
                probe_button.click()
        checker.check("网格方砖的「位置」发的是目录路径，不是模块标识",
                      revealed == [r"C:\tmp\demo.probe"], str(revealed))
        tile_heights = {b.height() for b in probe_tile.findChildren(QPushButton)}
        checker.check("方砖上的按钮也统一高度", len(tile_heights) == 1, str(sorted(tile_heights)))
        # **两个页面的按钮要一样大。** 之前流程卡片 32px、模块方砖 22px，字号也不一样 ——
        # 同一类东西在两个页面长得不同，一眼就看出来了。度量现在收在 theme 里。
        flow_card = window.workflow_list.card_for(
            next(e for e in window.library.entries if e.path.name == "demo.json").path
        )
        checker.check("「我的模块」和「我的流程」的按钮同高",
                      tile_heights == {flow_card.run_button.height()},
                      f"模块 {sorted(tile_heights)} vs 流程 {flow_card.run_button.height()}")
        checker.check("同宽",
                      {b.width() for b in probe_tile.findChildren(QPushButton)}
                      == {b.width() for b in (flow_card.run_button, flow_card.edit_button,
                                              flow_card.reveal_button, flow_card.delete_button)},
                      f"模块 {sorted({b.width() for b in probe_tile.findChildren(QPushButton)})} "
                      f"vs 流程 {flow_card.run_button.width()}")
        probe_tile.deleteLater()

        # 这一条以前**完全没有覆盖**：运行中只是把按钮禁掉、显示个"运行中"徽标，
        # 用户根本没有停止的入口。所以这里从"能不能点"一路测到"点完真的停"。
        # **明确挑一条能跑的**，不要用 entries[0]。它是按修改时间排的，哪条最新取决于
        # 这个测试文件自己前面造过什么 —— 捡到一条校验不过的，失败会以"按钮没变成停止"
        # 这种完全误导的方式出现。测试要自己控制前置条件。
        runnable = next(e for e in window.library.entries if e.path.name == "demo.json")
        idle_card = window.workflow_list.card_for(runnable.path)
        checker.check("空闲时按钮写着「运行」", idle_card.run_button.text() == "运行",
                      idle_card.run_button.text())
        checker.check("空闲时可以点", idle_card.run_button.isEnabled())
        # **同一行里的按钮必须一样高。** 它们的样式来自三处（运行绿色实心、删除红色描边、
        # 编辑和位置走默认），padding 只要有一点差别排在一起就参差不齐 —— 已经出过两次。
        button_heights = {
            idle_card.run_button.height(),
            idle_card.edit_button.height(),
            idle_card.reveal_button.height(),
            idle_card.delete_button.height(),
        }
        checker.check("卡片上四个按钮一样高",
                      len(button_heights) == 1, str(sorted(button_heights)))
        checker.check("高度是钉死的那个值，不是被样式凑出来的",
                      button_heights == {32}, str(sorted(button_heights)))
        card_buttons = (idle_card.run_button, idle_card.edit_button,
                        idle_card.reveal_button, idle_card.delete_button)
        button_widths = {b.width() for b in card_buttons}
        # 允许 1 像素的差：302px 减去间距和边距，再被 4 除往往除不尽，Qt 会把余数
        # 分给其中一两个。肉眼看不出来，钉死相等只会让测试无缘无故地红。
        checker.check("四个按钮**等宽**（等距平铺整行，删除不再单独隔开）",
                      max(button_widths) - min(button_widths) <= 1, str(sorted(button_widths)))
        checker.check("而且真的占满了这一行（不是挤在左边）",
                      sum(b.width() for b in card_buttons) + 6 * 3 >= idle_card.width() - 32,
                      f"{sum(b.width() for b in card_buttons)} vs 卡片宽 {idle_card.width()}")

        # 先等前面可能还在跑的流程收尾 —— 否则 run_entry 会直接 return，
        # 下面的断言就全测在空气上。
        for _ in range(80):
            pump(app, 0.2)
            if window._run_thread is None:
                break
        checker.check("开始之前没有别的流程在跑", window._run_thread is None)

        window.run_entry(runnable)
        pump(app, 0.4)
        checker.check("流程真的跑起来了（不然下面测的是空气）",
                      window._run_thread is not None)
        running_card = window.workflow_list.card_for(runnable.path)
        checker.check("跑起来之后就地变成「停止」（不另加一个按钮）",
                      running_card.run_button.text() == "停止", running_card.run_button.text())
        checker.check("而且**必须还能点** —— 否则用户就没法停了",
                      running_card.run_button.isEnabled())
        others = [c for path, c in window.workflow_list._cards.items()
                  if path != runnable.path]
        checker.check("别的条目被禁掉（同时只允许跑一条）",
                      all(not c.run_button.isEnabled() for c in others),
                      str([c.run_button.isEnabled() for c in others]))

        running_card.run_button.click()
        for _ in range(80):
            pump(app, 0.2)
            if window._run_thread is None:
                break
        checker.check("点了「停止」之后真的停下了", window._run_thread is None)
        checker.check("按钮回到「运行」", running_card.run_button.text() == "运行",
                      running_card.run_button.text())
        checker.check("别的条目恢复可点",
                      all(c.run_button.isEnabled() or not c.entry.ok for c in others),
                      str([c.run_button.isEnabled() for c in others]))

        # -- 增：点「新建模块」直接开到代码界面 -------------------------------
        window.sidebar.action_new_module.click()
        pump(app, 0.3)
        checker.check("直接切到了模块代码界面（没有先弹一个对话框）",
                      window.mode == MODE_MODULE, window.mode)
        # 进模块编辑器时高亮的是操作区的「新建模块」按钮，不是任何导航项
        checker.check("「新建模块」按钮被点亮",
                      window.sidebar.action_new_module.isChecked())
        checker.check("三个导航项都松开了（不会两个同时亮）",
                      not window.sidebar.nav_workflows.isChecked()
                      and not window.sidebar.nav_plugins.isChecked()
                      and not window.sidebar.nav_custom.isChecked(),
                      f"wf={window.sidebar.nav_workflows.isChecked()}"
                      f" pl={window.sidebar.nav_plugins.isChecked()}"
                      f" my={window.sidebar.nav_custom.isChecked()}")
        checker.check("窗口标题是新建模块", "新建模块" in window.windowTitle(), window.windowTitle())
        editor = window.module_editor
        checker.check("代码框里预置了一个能跑的骨架",
                      "from myautowork import" in editor.code.toPlainText())
        checker.check("骨架本身就能过校验", not editor.problems(), str(editor.problems()))

        editor.id_edit.setText("win.uia")
        pump(app, 0.05)
        checker.check("标识撞车时拦住并给出原因",
                      any("已经有一个模块" in p for p in editor.problems()),
                      str(editor.problems()))
        checker.check("有问题时保存按钮禁用", not editor.save_button.isEnabled())

        editor.id_edit.setText("user.second")
        editor.name_edit.setText("第二个模块")
        editor.code.setPlainText("def broken(:\n    pass\n")
        pump(app, 0.4)  # 等防抖定时器
        problems = editor.problems()
        checker.check("语法错误被指出来、并且带行号",
                      any("语法错误" in p and "第 1 行" in p for p in problems), str(problems))
        checker.check("语法错误时保存按钮禁用", not editor.save_button.isEnabled())

        editor.code.setPlainText(
            plugin_template(plugin_id="user.second", name="第二个模块", description="手写的")
        )
        pump(app, 0.4)
        checker.check("改好之后校验通过", not editor.problems(), str(editor.problems()))
        editor.save()
        pump(app, 0.5)
        checker.check("模块真的建出来了",
                      (isolated_plugins / "user.second" / "main.py").is_file()
                      and (isolated_plugins / "user.second" / "manifest.json").is_file())
        checker.check("页面上立刻多了一行", "user.second" in window.custom_view._rows,
                      str(sorted(window.custom_view._rows)))
        checker.check("保存后从「新建」切成「编辑」（再存是覆盖，不会说重名）",
                      editor.editing == "user.second", str(editor.editing))

        # -- 改：能打开已有模块改代码 -----------------------------------------
        checker.check("内置模块不给改代码", not editor.load("win.uia"))
        checker.check("能打开自己写的模块改代码", editor.load("user.second"))
        checker.check("代码框里装的是它自己的代码",
                      "第二个模块" in editor.code.toPlainText())
        checker.check("编辑已有模块时标识是锁住的", editor.id_edit.isReadOnly())
        checker.check("编辑已有模块时「新建模块」也是亮的（同一个界面、同一件事）",
                      window.sidebar.action_new_module.isChecked())
        checker.check("编辑时导航项同样都松开",
                      not window.sidebar.nav_custom.isChecked())

        shot_editor = docs_dir / "screenshot-module-editor.png"
        checker.check("模块代码界面截图已写出", window.grab().save(str(shot_editor)), str(shot_editor))

        # 离开编辑器就该熄灭（放在截图之后，别把截图拍到别的页面上）
        window.sidebar.nav_workflows.click()
        pump(app, 0.25)
        checker.check("回到流程列表后「新建模块」熄灭",
                      not window.sidebar.action_new_module.isChecked())
        checker.check("并且「我的流程」亮起来",
                      window.sidebar.nav_workflows.isChecked())
        # 窗口标题要在这个页面上才测得到 —— 它跟着当前页面走
        checker.check("回到流程页后窗口标题是「我的流程」",
                      window.windowTitle() == "我的流程", window.windowTitle())

        # -- 改：重命名只动显示名，标识和目录都不动 --------------------------
        original_get_text = QInputDialog.getText
        QInputDialog.getText = staticmethod(  # type: ignore[assignment]
            lambda *args, **kwargs: ("改过名的工具", True)
        )
        try:
            window._rename_module("user.second")
            pump(app, 0.35)
        finally:
            QInputDialog.getText = original_get_text  # type: ignore[assignment]

        checker.check("重命名后注册表里的名字变了",
                      window.registry.manifests["user.second"].name == "改过名的工具",
                      window.registry.manifests["user.second"].name)
        checker.check("标识和目录都没动（流程不会因此失效）",
                      (isolated_plugins / "user.second").is_dir()
                      and window.registry.has("user.second"))
        checker.check("manifest 文件里也写进去了",
                      "改过名的工具" in (isolated_plugins / "user.second" / "manifest.json")
                      .read_text(encoding="utf-8"))
        checker.check("左侧栏跟着更新了",
                      "改过名的工具" in card_texts(window.custom_view.row_for("user.second")),
                      card_texts(window.custom_view.row_for("user.second")))

        try:
            window.registry.rename("win.uia", "想改内置的")
            checker.check("内置模块拒绝改名", False, "居然改成功了")
        except MyAutoWorkError as exc:
            checker.check("内置模块拒绝改名", "内置" in str(exc), str(exc))
        checker.check("拒绝之后内置模块名字没变",
                      window.registry.manifests["win.uia"].name == "界面元素",
                      window.registry.manifests["win.uia"].name)

        # 拒绝删除的情况：内置的、以及不存在的
        try:
            window.registry.remove("win.uia")
            checker.check("内置模块拒绝删除", False, "居然删成功了")
        except MyAutoWorkError as exc:
            checker.check("内置模块拒绝删除", "内置" in str(exc), str(exc))
        checker.check("被拒绝之后内置模块还在", window.registry.has("win.uia"))

        window.sidebar.nav_workflows.click()
        pump(app, 0.2)
        checker.check("能看到触发器", "手动触发" in all_text)

        window.sidebar.nav_workflows.click()
        pump(app, 0.2)
        checker.check("能切回流程列表", window.mode == MODE_WORKFLOWS, window.mode)

        # -- 6. 进入编辑器 ----------------------------------------------------
        # -- 5c. 清单 ----------------------------------------------------------
        checker.section("5c) 我的清单：列表页 + 独立编辑器")
        checker.check("导航里是「我的清单」",
                      hasattr(window.sidebar, "nav_checklist")
                      and window.sidebar.nav_checklist.text() == "我的清单")
        checker.check("操作列有「新建清单」",
                      hasattr(window.sidebar, "action_new_checklist")
                      and window.sidebar.action_new_checklist.text() == "新建清单")
        window.sidebar.nav_checklist.click()
        pump(app, 0.25)
        checker.check("切到了我的清单页", window.mode == MODE_CHECKLIST, window.mode)
        checker.check("窗口标题跟着变", "我的清单" in window.windowTitle(), window.windowTitle())

        checklist = window.checklist_view
        editor = window.checklist_editor

        # 列表页**只列清单**，不显示流程勾选框 —— 那属于编辑器
        checker.check("一条清单都没有时就是空的（不会自动补一条默认的）",
                      checklist._items == {}, str(sorted(checklist._items)))
        checker.check("空的时候有明确的提示，不是一片空白",
                      any("还没有清单" in label.text()
                          for label in checklist.findChildren(QLabel)),
                      "没找到空状态提示")
        checker.check("列表页没有流程勾选区（拆到编辑器里了）",
                      not hasattr(checklist, "select_all")
                      and not hasattr(checklist, "set_entries")
                      and not hasattr(checklist, "checked_names"))
        checker.check("编辑器才有勾选区",
                      hasattr(editor, "select_all") and hasattr(editor, "picked_names"))

        shot_checklist = docs_dir / "screenshot-checklist.png"
        checker.check("我的清单截图已写出", window.grab().save(str(shot_checklist)), str(shot_checklist))

        # 新建走**独立页面**，和「新建模块」一个路子
        checklist.new_button.click()
        pump(app, 0.3)
        checker.check("新建清单开的是独立页面，不是就地输入行",
                      window.mode == MODE_CHECKLIST_EDIT, window.mode)
        checker.check("窗口标题是新建清单", "新建清单" in window.windowTitle(), window.windowTitle())
        checker.check("编辑器里导航项都松开了（二级页面）",
                      not window.sidebar.nav_checklist.isChecked()
                      and not window.sidebar.nav_workflows.isChecked())
        checker.check("编辑器列出了全部流程", len(editor._rows) == 3, str(sorted(editor._rows)))
        checker.check("编辑器默认一条都没勾", editor.picked_names() == [])
        checker.check("没填名字时保存是禁用的", not editor.save_button.isEnabled())
        checker.check("坏文件那一行不能勾", not editor.row_for("broken.json").box.isEnabled())

        editor.name_edit.setText("早间例程")
        pump(app, 0.1)
        checker.check("填了名字就能保存", editor.save_button.isEnabled())
        editor.select_all.click()
        pump(app, 0.1)
        checker.check("全选只勾得上能跑的",
                      set(editor.picked_names()) == {"demo.json", "report.json"},
                      str(editor.picked_names()))
        checker.check("统计跟着更新", "已勾选 2 条" in editor.summary.text(), editor.summary.text())

        shot_editor = docs_dir / "screenshot-checklist-editor.png"
        checker.check("清单编辑器截图已写出", window.grab().save(str(shot_editor)), str(shot_editor))

        editor.save()
        pump(app, 0.3)
        checker.check("保存后回到我的清单页", window.mode == MODE_CHECKLIST, window.mode)
        checker.check("新清单出现在列表里", "早间例程" in checklist._items,
                      str(sorted(checklist._items)))
        checker.check("列表上显示它的条数",
                      "2 条流程" in checklist.checklist_row_for("早间例程").counts.text(),
                      checklist.checklist_row_for("早间例程").counts.text())
        checker.check("有内容的清单运行按钮可用",
                      checklist.checklist_row_for("早间例程").run_button.isEnabled())

        # **不会自动补一条「默认清单」。** 一条都没有时就是空的，页面上有明确的空状态提示；
        # 凭空冒出一条用户没建过的东西，比空着更让人困惑 —— 他会以为那是自己建的。
        checker.check("不会自动建一条「默认清单」",
                      "默认清单" not in checklist._items, str(sorted(checklist._items)))

        # 下面要用到一条**空**清单，所以这里自己建一条 —— 测试要自己控制前置条件，
        # 不能靠"程序应该会替我准备一条"（那正是刚才被去掉的行为）。
        checklist.new_button.click()
        pump(app, 0.3)
        editor.name_edit.setText("默认清单")
        editor.save()
        pump(app, 0.3)
        checker.check("空清单的运行按钮是禁用的",
                      not checklist.checklist_row_for("默认清单").run_button.isEnabled())

        # 问程序自己要路径，不要写死 —— 位置改过两次了（根上 → config/ → 和 workflows 平级），
        # 写死的话每改一次这里都要跟着断一次。
        data = json.loads(window._checklist_path().read_text(encoding="utf-8"))
        checker.check("清单落在了 checklists/ 目录里（和 workflows 平级）",
                      window._checklist_path().parent.name == "checklists"
                      and window._checklist_path().parent.parent == window.workflows_dir.parent,
                      str(window._checklist_path()))
        checker.check("清单写进了 checklists.json",
                      set(data["checklists"]["早间例程"]) == {"demo.json", "report.json"},
                      str(data["checklists"]))

        # 重名要被拦住，而且停在编辑器上不跳走
        checklist.new_button.click()
        pump(app, 0.3)
        editor.name_edit.setText("早间例程")
        editor.save()
        pump(app, 0.2)
        checker.check("重名清单不会被建出来", len(checklist._items) == 2,
                      str(sorted(checklist._items)))
        checker.check("重名时停在编辑器上", window.mode == MODE_CHECKLIST_EDIT, window.mode)
        checker.check("并且给了提示", "已经" in editor.name_edit.placeholderText(),
                      editor.name_edit.placeholderText())
        editor.cancel_button.click()
        pump(app, 0.3)
        checker.check("取消回到列表", window.mode == MODE_CHECKLIST, window.mode)

        # 编辑：改名 + 改内容
        checklist.checklist_row_for("默认清单").edit_button.click()
        pump(app, 0.3)
        checker.check("点「编辑」进了编辑器", window.mode == MODE_CHECKLIST_EDIT, window.mode)
        checker.check("编辑器里预填了原名", editor.name_edit.text() == "默认清单",
                      editor.name_edit.text())
        editor.name_edit.setText("日常")
        editor.row_for("demo.json").set_checked(True)
        pump(app, 0.1)
        editor.save()
        pump(app, 0.3)
        checker.check("改名 + 改内容都生效",
                      "日常" in checklist._items and "默认清单" not in checklist._items,
                      str(sorted(checklist._items)))
        checker.check("改名后条数对",
                      "1 条流程" in checklist.checklist_row_for("日常").counts.text(),
                      checklist.checklist_row_for("日常").counts.text())

        # 列表里直接点「运行」：不用先切过去
        checklist.checklist_row_for("日常").run_button.click()
        pump(app, 1.0)
        checker.check("点清单行上的「运行」就开跑了", window._run_thread is not None)
        checker.check("运行期间停止按钮可用", checklist.stop_button.isEnabled())
        checker.check("运行期间清单行被禁用",
                      not checklist.checklist_row_for("早间例程").isEnabled())

        for _ in range(60):  # 等它跑完（demo 里有 1.5 秒的等待）
            pump(app, 0.2)
            if window._run_thread is None:
                break

        checker.check("跑完之后那一行标了结果",
                      "成功" in checklist.checklist_row_for("日常").status.text(),
                      checklist.checklist_row_for("日常").status.text())
        checker.check("跑完之后清单行恢复可用",
                      checklist.checklist_row_for("早间例程").isEnabled())
        checker.check("日志里记了清单开始与结束",
                      "开始执行清单" in window.log_panel.log_view.toPlainText()
                      and "执行完毕" in window.log_panel.log_view.toPlainText())
        checker.check("清单队列已清空", window._checklist_queue == [])

        # 删除
        original_question = QMessageBox.question
        QMessageBox.question = staticmethod(  # type: ignore[assignment]
            lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        try:
            window._delete_checklist("早间例程")
            pump(app, 0.3)
        finally:
            QMessageBox.question = original_question  # type: ignore[assignment]
        checker.check("删除清单生效（流程本身没删）", "早间例程" not in checklist._items,
                      str(sorted(checklist._items)))

        # -- 5e. 深色 / 浅色主题 -------------------------------------------------
        checker.section("5e) 主题切换")
        theme_mod = __import__("host.theme", fromlist=["theme"])
        checker.check("「视图」菜单里有主题项",
                      window.action_theme in window.view_menu.actions())
        checker.check("默认是深色", theme_mod.mode() == "dark", theme_mod.mode())
        dark_bg = theme_mod.BACKGROUND

        window.action_theme.setChecked(True)
        pump(app, 0.4)
        checker.check("切到了浅色", theme_mod.mode() == "light", theme_mod.mode())
        checker.check("画布背景真的变浅了", theme_mod.BACKGROUND != dark_bg,
                      f"{dark_bg} -> {theme_mod.BACKGROUND}")
        checker.check("正文颜色也跟着反了", theme_mod.NODE_TEXT != "#dfe4ea",
                      theme_mod.NODE_TEXT)
        checker.check("语义色没被动（成功还是那个绿）",
                      theme_mod.STATE_SUCCESS == "#4fae63", theme_mod.STATE_SUCCESS)

        window.sidebar.nav_workflows.click()
        pump(app, 0.3)
        shot_light = docs_dir / "screenshot-light.png"
        checker.check("浅色主题截图已写出", window.grab().save(str(shot_light)), str(shot_light))

        window.action_theme.setChecked(False)
        pump(app, 0.4)
        checker.check("能切回深色", theme_mod.mode() == "dark", theme_mod.mode())
        checker.check("背景回到了原来的值", theme_mod.BACKGROUND == dark_bg,
                      theme_mod.BACKGROUND)
        checker.check("页面重建后仍然是深色的卡片",
                      window.workflow_list._cards and
                      all(card.isVisible() or True for card in window.workflow_list._cards.values()))

        # -- 5d. 标题栏的侧边栏开关 --------------------------------------------
        checker.section("5d) 标题栏最左边收/放左侧栏")
        checker.check("标题栏有那个按钮",
                      hasattr(window.title_bar, "sidebar_button")
                      and window.title_bar.sidebar_button.text() == "☰")

        # 原生缩放的坐标换算。离屏测试里窗口 DPR 恒为 1，跑不出真实缩放，所以直接
        # 喂一个 1.25 进去 —— 这台开发机就是 125%，之前正是这里没换算，导致
        # "边框拖不动 + 最大化按钮点不动"。
        centre = window.mapToGlobal(window.rect().center())
        logical = window._native_to_local(round(centre.x() * 1.25), round(centre.y() * 1.25), ratio=1.25)
        checker.check("原生坐标会按 DPR 换算成逻辑坐标",
                      abs(logical.x() - window.rect().center().x()) <= 1
                      and abs(logical.y() - window.rect().center().y()) <= 1,
                      f"{logical} vs {window.rect().center()}")
        checker.check("不换算的话会偏出去（回归守卫）",
                      window._native_to_local(round(centre.x() * 1.25),
                                              round(centre.y() * 1.25),
                                              ratio=1.0).x() > window.rect().center().x() + 20)
        checker.check("标题栏上的窗口按钮不会被当成缩放热区",
                      window._over_title_button(window.title_bar.max_button.mapTo(
                          window, QPoint(5, 5)).x()))
        window.sidebar.nav_workflows.click()
        pump(app, 0.2)
        checker.check("默认展开", window.sidebar.isVisible())
        window.title_bar.sidebar_button.click()
        pump(app, 0.25)
        checker.check("点一下收起", not window.sidebar.isVisible())
        checker.check("收起后主区还在", window.stack.isVisible())
        window.title_bar.sidebar_button.click()
        pump(app, 0.25)
        checker.check("再点一下展开", window.sidebar.isVisible())
        checker.check("展开后宽度正常", window.sidebar.width() > 100,
                      str(window.sidebar.width()))

        window.sidebar.nav_workflows.click()
        pump(app, 0.2)

        checker.section("6) 从列表进入编辑器")
        edit_card = window.workflow_list.card_for(fixtures["demo"])
        assert edit_card is not None
        edit_card.edit_button.click()
        pump(app, 0.3)
        checker.check("切到了编辑器", window.mode == MODE_EDITOR, window.mode)
        checker.check("侧边栏收起（左边让给模块面板）", not window.sidebar.isVisible())
        checker.check("工具栏出现", window.toolbar.isVisible())
        checker.check("模块面板出现", window.palette_panel.isVisible())
        checker.check("属性面板出现", window.inspector.isVisible())
        # 光看 isVisible() 不够：分隔器可能把宽度留成 0，控件"可见"但一片空白。
        checker.check("模块面板真的有宽度", window.palette_panel.width() > 100,
                      str(window.palette_panel.width()))
        checker.check("属性面板真的有宽度", window.inspector.width() > 100,
                      str(window.inspector.width()))
        checker.check("侧边栏让位（模块面板贴到最左边）",
                      window.palette_panel.mapTo(window, window.palette_panel.rect().topLeft()).x() == 0,
                      str(window.palette_panel.mapTo(window, window.palette_panel.rect().topLeft())))
        checker.check("编辑器里标题栏依然横跨全宽",
                      window.title_bar.width() >= window.width() - 4,
                      f"titlebar={window.title_bar.width()} window={window.width()}")
        checker.check("画布上有 4 个节点", len(window.scene.node_items) == 4,
                      str(sorted(window.scene.node_items)))
        checker.check("画布上有 4 条连线", len(window.scene.edge_items) == 4)
        checker.check("窗口标题变成文件名", "demo.json" in window.windowTitle(), window.windowTitle())
        checker.check("没有未保存改动", not window._dirty)

        # -- 7. 节点端口来自插件声明 ------------------------------------------
        checker.section("7) 节点端口由插件声明决定")
        n3 = window.scene.node_item("n3")
        checker.check("n3 存在", n3 is not None)
        assert n3 is not None
        checker.check(
            "n3 的输入端口齐全",
            set(n3.input_ports) == {"in", "path", "content", "encoding", "append", "create_dirs"},
            str(sorted(n3.input_ports)),
        )
        checker.check(
            "n3 的输出端口齐全",
            set(n3.output_ports) == {"success", "error", "path", "bytes", "lines", "created"},
            str(sorted(n3.output_ports)),
        )

        # -- 多路分支：分支出口要**当场**出现在画布上 ---------------------------
        # 这条盯着一个很容易再犯的错：画布上 params 存的是 ParamValue（带 mode/value），
        # 流程文件里是裸值。解析函数只认裸值的话，界面上填了四条分支画布上一条都不会多 ——
        # 而"用裸值去测"是测不出来的（第一版就是这么漏过去的）。
        switch_spec = {
            "id": "switch",
            "name": "多路分支",
            "category": "流程控制",
            "kind": "action",
            "branches": "cases",
            "inputs": {"cases": {"kind": "text", "label": "分支"}},
            "outputs": {"matched": {"kind": "string", "label": "走了哪条"}},
        }
        branch_node = NodeItem(
            "sw", "core.flow", "switch", switch_spec,
            params={"cases": ParamValue(mode=MODE_LITERAL, value="")},
        )
        checker.check("没填分支时只有 success / error",
                      set(branch_node.output_ports) == {"success", "error", "matched"},
                      str(sorted(branch_node.output_ports)))
        branch_node.params["cases"] = ParamValue(mode=MODE_LITERAL, value="批发\n零售\n退单\n其他")
        checker.check("改了参数会报出被删掉的出口", branch_node.refresh_ports() == [])
        checker.check("填了四条分支，画布上就多出四个出口",
                      set(branch_node.output_ports)
                      == {"批发", "零售", "退单", "其他", "error", "matched"},
                      str(sorted(branch_node.output_ports)))
        checker.check("分支节点**没有** success —— 分支本身就是走通的那条路",
                      "success" not in branch_node.output_ports,
                      str(sorted(branch_node.output_ports)))
        branch_node.params["cases"] = ParamValue(mode=MODE_LITERAL, value="大客户\n小客户")
        removed = branch_node.refresh_ports()
        checker.check("改成两条，旧的四个出口报为「已删除」（调用方据此删悬挂的边）",
                      set(removed) == {"批发", "零售", "退单", "其他"}, str(removed))
        checker.check("两条分支的出口",
                      set(branch_node.output_ports) == {"大客户", "小客户", "error", "matched"},
                      str(sorted(branch_node.output_ports)))
        branch_node.params["cases"] = ParamValue(mode=MODE_EXPR, value="{{ $x }}")
        branch_node.refresh_ports()
        checker.check("分支名是表达式时退回 success/error（编辑期算不出来，画不了出口）",
                      set(branch_node.output_ports) == {"success", "error", "matched"},
                      str(sorted(branch_node.output_ports)))

        # -- 多路分支：走**真实路径**（面板表单 → 画面节点）--------------------
        # 上面那条直接调 NodeItem，是抓不到真正那两个 bug 的：
        #   1. 画布上 params 存的是 ParamValue，解析函数只认裸值 → 填了也不出出口
        #   2. 面板直接写 item.params，不走 scene.set_param → 我加在 set_param 里的
        #      重建从来没被执行过
        # 所以这条必须**驱动表单控件**，让整条链路真的跑一遍。
        # 用**独立的一次性场景**，不动主界面 —— 重置 window 的场景会把后面测试引用的
        # 节点一起删掉（试过一次：报 "Internal C++ object (NodeItem) already deleted"）。
        probe_scene = WorkflowScene(window.registry)
        probe_inspector = InspectorPanel(probe_scene)
        real_switch = probe_scene.add_node("core.flow", "switch", QPointF(0.0, 0.0))
        probe_inspector.show_node(real_switch)
        pump(app, 0.15)
        checker.check("刚拖进来时是默认的 success / error",
                      "success" in real_switch.output_ports,
                      str(sorted(real_switch.output_ports)))
        probe_inspector.form.rows["cases"].value_editor.set_value("批发\n零售\n退单\n其他")
        pump(app, 0.2)
        checker.check("在属性面板里填四条分支，画布上当场多出四个出口",
                      {"批发", "零售", "退单", "其他"} <= set(real_switch.output_ports),
                      str(sorted(real_switch.output_ports)))
        checker.check("分支节点上没有 success 了",
                      "success" not in real_switch.output_ports,
                      str(sorted(real_switch.output_ports)))
        probe_inspector.form.rows["cases"].value_editor.set_value("大客户\n小客户")
        pump(app, 0.2)
        checker.check("改成两条，旧的四个出口消失、新的两个出现",
                      {"大客户", "小客户"} <= set(real_switch.output_ports)
                      and not ({"批发", "零售", "退单", "其他"} & set(real_switch.output_ports)),
                      str(sorted(real_switch.output_ports)))
        probe_scene.deleteLater()
        probe_inspector.deleteLater()
        n1 = window.scene.node_item("n1")
        assert n1 is not None
        checker.check("触发器没有执行入口", "in" not in n1.input_ports)

        data_edges = [e for e in window.scene.edge_items if e.kind == "data"]
        checker.check("恰好 1 条数据线", len(data_edges) == 1)
        if data_edges:
            edge = data_edges[0]
            checker.check(
                "数据线是 n2.finished_at -> n3.content",
                edge.src_port.owner.node_id == "n2"
                and edge.src_port.name == "finished_at"
                and edge.dst_port.owner.node_id == "n3"
                and edge.dst_port.name == "content",
            )

        # -- 8. schema → 控件 -------------------------------------------------
        checker.section("8) schema 驱动的表单生成")
        window.scene.clearSelection()
        before_params = {k: (v.mode, v.value) for k, v in n3.params.items()}
        n3.setSelected(True)
        app.processEvents()
        rows = window.inspector.form.rows
        checker.check("属性面板列出了 5 个数据输入", len(rows) == 5, str(sorted(rows)))
        after_params = {k: (v.mode, v.value) for k, v in n3.params.items()}
        checker.check("重建表单没有改动节点参数（回归守卫）",
                      before_params == after_params, f"{before_params} -> {after_params}")

        path_editor = rows["path"].value_editor
        browse = getattr(path_editor, "button", None)
        checker.check("File 类型自动长出「浏览」按钮",
                      browse is not None and browse.text() == "浏览", type(path_editor).__name__)
        checker.check("Text 类型渲染成多行控件",
                      type(rows["content"].value_editor).__name__ == "_MultiLineEdit")
        encoding_editor = rows["encoding"].value_editor
        checker.check(
            "Enum 类型渲染成下拉且选项来自声明",
            hasattr(encoding_editor, "combo")
            and [encoding_editor.combo.itemText(i) for i in range(encoding_editor.combo.count())]
            == ["utf-8", "utf-8-sig", "gbk"],
        )
        checker.check("Bool 类型渲染成复选框", hasattr(rows["append"].value_editor, "box"))

        element_editor = create_value_editor("element", {"kind": "element", "picker": "uia"})
        element_button = getattr(element_editor, "button", None)
        checker.check("Element 类型自动长出瞄准镜按钮",
                      element_button is not None and element_button.text() == "🎯")
        element_editor.set_value("btnExport")
        checker.check("Element 能解析简写为选择器",
                      element_editor.value() == {"automation_id": "btnExport"})

        # -- 9. 取值来源 ------------------------------------------------------
        checker.section("9) 取值来源：常量 / 表达式 / 连线")
        content_mode = rows["content"].mode_combo
        upstream_item = content_mode.model().item(content_mode.findData("upstream"))
        checker.check("content 有连线，所以「连线」模式可用",
                      upstream_item is not None and upstream_item.isEnabled())
        path_mode = rows["path"].mode_combo
        path_upstream = path_mode.model().item(path_mode.findData("upstream"))
        checker.check("path 没有连线，所以「连线」模式被禁用",
                      path_upstream is not None and not path_upstream.isEnabled())
        checker.check("path 当前是表达式模式", rows["path"]._current_mode() == "expr")
        checker.check("表达式内容原样显示",
                      rows["path"].expr_edit.text() == "out/demo_{{$date}}.txt")
        checker.check("连线模式显示了上游来源",
                      "n2.finished_at" in rows["content"].upstream_label.text())

        checker.section("10) 编辑参数会同步回模型并标记未保存")
        rows["encoding"].value_editor.combo.setCurrentIndex(1)
        pump(app, 0.05)
        checker.check("参数写回了节点", n3.params["encoding"].value == "utf-8-sig")
        checker.check("文档被标记为有改动", window._dirty)
        rows["encoding"].value_editor.combo.setCurrentIndex(0)
        pump(app, 0.05)

        checker.section("11) 连线规则会拦住不合法的连接")
        n4 = window.scene.node_item("n4")
        assert n4 is not None
        ok, reason = window.scene.can_connect(n1.output_ports["success"], n3.input_ports["path"])
        checker.check("执行端口连不到数据端口", not ok, reason)
        ok, reason = window.scene.can_connect(n3.output_ports["bytes"], n3.input_ports["path"])
        checker.check("不能连到同一个节点", not ok, reason)
        ok, reason = window.scene.can_connect(n3.output_ports["lines"], n4.input_ports["path"])
        checker.check("数字连到文件路径不兼容", not ok, reason)
        checker.check("拒绝原因说得清类型", "integer" in reason and "file" in reason, reason)
        ok, reason = window.scene.can_connect(n3.output_ports["path"], n4.input_ports["path"])
        checker.check("字符串连到文件路径可以连", ok, reason)

        checker.section("12) 编辑器截图")
        window.scene.clearSelection()
        n3.setSelected(True)
        window.view.fit_content()
        pump(app, 0.35)
        shot_editor = docs_dir / "screenshot-editor.png"
        checker.check("整窗截图已写出", window.grab().save(str(shot_editor)), str(shot_editor))
        shot_canvas = docs_dir / "screenshot-canvas.png"
        checker.check("画布截图已写出", window.view.grab().save(str(shot_canvas)), str(shot_canvas))

        # -- 13. 返回列表 -----------------------------------------------------
        checker.section("13) 返回流程列表")
        window.action_home.trigger()
        pump(app, 0.2)
        checker.check("回到流程列表", window.mode == MODE_WORKFLOWS, window.mode)
        checker.check("侧边栏又出现", window.sidebar.isVisible())
        checker.check("工具栏又隐藏", not window.toolbar.isVisible())
        checker.check("模块面板又隐藏", not window.palette_panel.isVisible())
        checker.check("侧边栏又回到最左边",
                      window.sidebar.isVisible()
                      and window.sidebar.mapTo(window, window.sidebar.rect().topLeft()).x() == 0,
                      str(window.sidebar.mapTo(window, window.sidebar.rect().topLeft())))
        checker.check("列表卡片还在", len(window.workflow_list._cards) == 3)

        # -- 14. 保存 / 读取往返 ----------------------------------------------
        checker.section("14) 保存 / 读取往返")
        edit_card = window.workflow_list.card_for(fixtures["demo"])
        assert edit_card is not None
        edit_card.edit_button.click()
        pump(app, 0.3)

        roundtrip = tmpdir / "roundtrip.json"
        window.scene.set_param("n3", "encoding", ParamValue(mode="literal", value="gbk"))
        save_workflow(window.scene.to_workflow(), roundtrip)
        reloaded = load_workflow(roundtrip)
        checker.check("节点数一致", len(reloaded.nodes) == len(window.scene.node_items))
        checker.check("连线数一致", len(reloaded.edges) == len(window.scene.edge_items))
        checker.check("参数改动被保存", reloaded.nodes["n3"].param_value("encoding") == "gbk")
        checker.check("表达式参数被保存", reloaded.nodes["n3"].params["path"].mode == "expr")

        saved_into_library = fixtures["dir"] / "roundtrip.json"
        window._write(saved_into_library)
        pump(app, 0.2)
        checker.check("保存后首页立刻多了这条", len(window.workflow_list._cards) == 4,
                      str(sorted(p.name for p in window.workflow_list._cards)))
        checker.check("侧边栏统计也跟着更新",
                      "4 条流程" in window.sidebar.footer.text(), window.sidebar.footer.text())

        # -- 15. 缺失插件不应让整张图打不开 -----------------------------------
        checker.section("15) 缺失插件的节点以占位形式打开")
        broken_graph = tmpdir / "missing-plugin.json"
        broken_graph.write_text(
            json.dumps(
                {
                    "id": "broken-graph",
                    "name": "引用了没装的插件",
                    "nodes": [
                        {"id": "n1", "plugin": "core.trigger.manual", "action": "start", "pos": [0, 0]},
                        {"id": "n2", "plugin": "not.installed", "action": "ghost", "pos": [300, 0]},
                    ],
                    "edges": [{"from": "n1", "to": "n2", "kind": "exec"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        window.load_path(broken_graph)
        pump(app, 0.2)
        ghost = window.scene.node_item("n2")
        checker.check("缺失插件的节点仍然显示出来", ghost is not None)
        checker.check("并且被标记为 missing", ghost is not None and ghost.missing)
        checker.check("整张图没有因此打不开", len(window.scene.node_items) == 2)

        # -- 16. AI 对话（骨架）与自定义模块 -----------------------------------
        checker.section("16) AI 对话与自定义模块")
        checker.check("底部面板里有「AI 对话」页",
                      window.log_panel.indexOf(window.ai_panel) >= 0,
                      str(window.log_panel.count()))
        history = window.ai_panel.history
        checker.check("启动时有一句欢迎消息",
                      len(history) == 1 and history[0].role == "assistant",
                      str([m.role for m in history]))
        checker.check("欢迎消息说清了现在只是框架",
                      "框架" in history[0].text and "没有接" in history[0].text,
                      history[0].text[:60])

        window.ai_panel.input.setPlainText("帮我做一个每天导出报表的流程")
        window.ai_panel.send()
        # **等 busy 变回 False，不要用固定时长。** 后端现在跑在线程里，固定 pump 多少秒
        # 是在赌线程跑多快 —— 机器一忙就变成偶发失败。
        for _ in range(60):
            pump(app, 0.05)
            if not window.ai_panel.busy:
                break
        history = window.ai_panel.history
        checker.check("用户消息进了历史",
                      len(history) == 3 and history[1].role == "user", str(len(history)))
        checker.check("得到了助手回复",
                      history[2].role == "assistant" and len(history[2].text) > 10)
        checker.check("输入框被清空", window.ai_panel.input.toPlainText() == "")

        # 后端可替换 —— 这是"骨架"能不能用的关键：界面不需要为真模型改一行
        draft_code = plugin_template(
            plugin_id="demo.ai_made",
            name="AI 造的插件",
            description="由 AI 助手生成的示例模块",
            category="示例",
        )
        draft = PluginDraft(
            plugin_id="demo.ai_made",
            name="AI 造的插件",
            description="由 AI 助手生成的示例模块",
            category="示例",
            code=draft_code,
            provides={"actions": ["run"]},
        )

        def fake_backend(prompt: str, past: list[AiMessage]) -> AiMessage:
            return AiMessage(role="assistant", text=f"收到：{prompt}", draft=draft)

        window.ai_panel.set_backend(fake_backend)
        window.ai_panel.input.setPlainText("写一个插件")
        window.ai_panel.send()
        for _ in range(60):
            pump(app, 0.05)
            if not window.ai_panel.busy:
                break
        checker.check("换后端后立刻生效",
                      "收到：写一个插件" in window.ai_panel.history[-1].text,
                      window.ai_panel.history[-1].text)
        checker.check("回复带上了插件草稿", window.ai_panel.history[-1].draft is not None)

        # -- AI 设置：入口在「编辑」菜单里 --------------------------------------
        from host.ai_backend import AiError, ChatBackend  # noqa: PLC0415
        # AiSettings 已经在模块顶部导入过 —— **不要在这里再导入一次**：那会把它变成函数内的
        # 局部变量，而函数开头 MainWindow(..., ai_settings=AiSettings()) 就先用了它。
        from host.ai_settings import load_settings, save_settings  # noqa: PLC0415

        checker.check("「编辑」菜单里就是「设置」这一个入口",
                      window.action_ai_settings in window.menu_bar.actions()[1].menu().actions()
                      and window.action_ai_settings.text() == "设置…",
                      window.action_ai_settings.text())
        checker.check("配了 Ctrl+, 这个习惯键",
                      window.action_ai_settings.shortcut().toString() == "Ctrl+,",
                      window.action_ai_settings.shortcut().toString())

        # 地址拼接：三种写法都要认，否则用户会为了一个斜杠反复试
        checker.check("接口地址自动补 /chat/completions",
                      AiSettings(base_url="https://x/v1").chat_url
                      == "https://x/v1/chat/completions")
        checker.check("带结尾斜杠也认",
                      AiSettings(base_url="https://x/v1/").chat_url
                      == "https://x/v1/chat/completions")
        checker.check("已经写全了就不重复拼",
                      AiSettings(base_url="https://x/v1/chat/completions").chat_url
                      == "https://x/v1/chat/completions")
        checker.check("地址留空回落到默认",
                      "deepseek" in AiSettings(base_url="").chat_url)

        # **消息拼装：同一句话不能发两遍。**
        # AiPanel 是先记用户消息、再调后端的，history 最后一条就是 prompt；拼装时再追加
        # 一次的话，模型会收到两遍同样的话。它不报错，只会答得莫名其妙，很难往回查。
        backend = ChatBackend(AiSettings())
        history = [AiMessage(role="assistant", text="开场白"),
                   AiMessage(role="user", text="你好")]
        built = backend.messages("你好", history)
        checker.check("同一句用户消息只发一遍",
                      sum(1 for m in built if m["content"] == "你好") == 1,
                      str([m["content"] for m in built]))
        checker.check("系统提示词在最前面", built[0]["role"] == "system")
        checker.check("系统提示词里讲了 SDK（不然模型写不出能跑的插件）",
                      "manifest.json" in built[0]["content"]
                      and "@action" in built[0]["content"])

        # 没填密钥要当场说清楚、并且指路。环境变量也要先摘掉 —— 否则这一条会取决于
        # 跑测试那台机器设没设 MYAUTOWORK_AI_KEY。
        saved_key = os.environ.pop("MYAUTOWORK_AI_KEY", None)
        try:
            try:
                backend("你好", history)
            except AiError as exc:
                checker.check("没填密钥时明确报错、并且指路去设置里填",
                              "API Key" in str(exc) and "设置" in str(exc), str(exc))
            else:
                checker.check("没填密钥时明确报错、并且指路去设置里填", False,
                              "居然没报错就发出去了")
        finally:
            if saved_key is not None:
                os.environ["MYAUTOWORK_AI_KEY"] = saved_key

        # 设置的存读往返
        cfg = tmpdir / "ai" / "settings.json"
        save_settings(AiSettings(api_key="k-123", model="m-9", temperature=0.7), cfg)
        back = load_settings(cfg)
        checker.check("设置能存能读",
                      back.api_key == "k-123" and back.model == "m-9"
                      and abs(back.temperature - 0.7) < 1e-6,
                      f"{back.api_key}/{back.model}/{back.temperature}")
        checker.check("配置文件坏了也不让程序起不来",
                      load_settings(tmpdir / "没有这个文件.json").api_key == "")
        (tmpdir / "坏配置.json").write_text("{ 这不是 json", encoding="utf-8")
        checker.check("坏掉的配置回落到默认值",
                      load_settings(tmpdir / "坏配置.json").model == AiSettings().model)

        # 设置对话框：能构造、能取回值
        from host.ai_settings_dialog import SettingsDialog  # noqa: PLC0415

        # AI 现在是「设置」窗口里的一页，不是独立对话框 —— 探针打在那一页上。
        dialog = SettingsDialog(AiSettings(api_key="", model="m"), window)
        checker.check("设置窗口里 AI 是其中一个选项",
                      dialog.categories.count() >= 1
                      and dialog.categories.item(0).text() == "AI 助手",
                      str([dialog.categories.item(i).text()
                           for i in range(dialog.categories.count())]))
        ai_page = dialog.ai_page
        ai_page.model.setText("改过的模型")
        ai_page.system_prompt.setPlainText("你是助手")
        checker.check("设置窗口能取回界面上的值",
                      dialog.ai_settings().model == "改过的模型"
                      and dialog.ai_settings().system_prompt == "你是助手",
                      dialog.ai_settings().model)
        ai_page._probe_connection()
        checker.check("没填密钥时「测试连接」不去真连，直接提示",
                      ai_page._probe is None and "先填" in ai_page.test_result.text(),
                      ai_page.test_result.text())
        dialog.deleteLater()
        save_buttons = [b for b in window.ai_panel.findChildren(QPushButton)
                        if b.text() == "保存到插件目录"]
        checker.check("草稿卡片渲染出来了（有保存按钮）", len(save_buttons) >= 1,
                      str(len(save_buttons)))
        checker.check("保存按钮可用（草稿校验通过）",
                      bool(save_buttons) and save_buttons[0].isEnabled())

        # 拍一张能看清楚的：回到首页，临时把底部面板拉高
        window.log_panel.setCurrentWidget(window.ai_panel)
        window.back_to_list()
        pump(app, 0.25)
        original_sizes = window.main_splitter.sizes()
        window.main_splitter.setSizes([max(220, original_sizes[0] - 340), 480])
        pump(app, 0.4)
        shot_ai = docs_dir / "screenshot-ai.png"
        checker.check("AI 对话截图已写出", window.grab().save(str(shot_ai)), str(shot_ai))
        window.main_splitter.setSizes(original_sizes)
        pump(app, 0.2)

        # 草稿落盘：走界面的真实路径
        window._save_plugin_draft(draft)
        pump(app, 0.4)
        target = isolated_plugins / "demo.ai_made"
        checker.check("插件目录已创建",
                      (target / "manifest.json").is_file() and (target / "main.py").is_file(),
                      str(target))
        checker.check("注册表里立刻多了这个插件",
                      "demo.ai_made" in window.registry.manifests,
                      str(sorted(window.registry.manifests)))
        checker.check("但插件页里**没有**它（自己的模块不进那一页）",
                      window.plugin_view.plugin_count() == expected_plugins
                      and window.plugin_view.card_for("demo.ai_made") is None,
                      str(window.plugin_view.plugin_count()))
        checker.check("侧边栏统计跟着更新",
                      "4 条流程" in window.sidebar.footer.text()
                      and "插件" not in window.sidebar.footer.text(),
                      window.sidebar.footer.text())
        checker.check("日志里记了一笔",
                      "已创建插件 demo.ai_made" in window.log_panel.log_view.toPlainText())

    finally:
        window.confirm_on_close = False
        window.close()
        registry.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 守卫：整个测试只该动临时目录。仓库的 plugins/ 被写脏过一次，跑完对一下。
    leaked = {p.name for p in (REPO_ROOT / "plugins").iterdir()} - repo_plugins_before
    checker.check("没有往仓库的 plugins/ 里写东西", not leaked, str(sorted(leaked)))

    print(f"\n{'=' * 56}")
    print(f"通过 {checker.passed} 项，失败 {checker.failed} 项")
    if checker.failed == 0:
        print(f"截图：{docs_dir}")
    return 1 if checker.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
