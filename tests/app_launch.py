"""验证图形入口真的能启动。

    python tests/app_launch.py            # 无参数：停在首页列表
    python tests/app_launch.py editor     # 带流程参数：直接进编辑器

界面测试是直接组装控件的，绕过了 ``host.app.main()``。这个测试专门走真实入口。

**为什么分两次进程跑**：Qt 一个进程只允许有一个 ``QApplication``，而两个场景都要走
``main()``。用命令行参数挑场景，比在同一个进程里硬造第二个 QApplication 干净。

做法是换掉 ``QApplication.exec``，让它立刻返回 —— 不进入事件循环，因为这里没有人
去点按钮。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and "QT_QPA_FONTDIR" not in os.environ:
    _system_fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    if os.path.isdir(_system_fonts):
        os.environ["QT_QPA_FONTDIR"] = _system_fonts

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QStatusBar  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except (AttributeError, ValueError):  # pragma: no cover
    pass


class _HeadlessApplication(QApplication):
    """不进入事件循环的 QApplication。"""

    def exec(self) -> int:  # noqa: D102 - Qt 重写
        return 0


def _run_main(extra_args: list[str]) -> tuple[int, list]:
    """走真实的 host.app.main()，返回 (返回码, 创建出来的窗口列表)。"""
    import host.app as app_module

    created: list = []
    real_window = app_module.MainWindow

    class RecordingWindow(real_window):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    app_module.QApplication = _HeadlessApplication  # type: ignore[misc]
    app_module.MainWindow = RecordingWindow  # type: ignore[misc]

    return app_module.main(extra_args), created


def scenario_list() -> int:
    """不带参数启动：应该停在流程列表首页。"""
    from host.main_window import MODE_WORKFLOWS

    tmpdir = Path(tempfile.mkdtemp(prefix="myautowork-launch-"))
    failures = 0
    try:
        code, created = _run_main(["--no-history", "--workdir", str(tmpdir)])
        checks: list[tuple[str, bool, str]] = [("main() 正常返回 0", code == 0, str(code))]

        if not created:
            checks.append(("创建了主窗口", False, "没有创建"))
        else:
            window = created[0]
            checks += [
                ("启动后停在流程列表", window.mode == MODE_WORKFLOWS, window.mode),
                ("窗口标题不含软件名", "myautowork" not in window.windowTitle(),
                 window.windowTitle()),
                ("无边框窗口", bool(window.windowFlags() & Qt.WindowType.FramelessWindowHint), ""),
                (
                    "扫描到了 demo.json",
                    any(entry.path.name == "demo.json" for entry in window.library.entries),
                    str([e.path.name for e in window.library.entries]),
                ),
                (
                    "首页为每条流程建了卡片",
                    len(window.workflow_list._cards) == len(window.library.entries),
                    f"{len(window.workflow_list._cards)} vs {len(window.library.entries)}",
                ),
                ("侧边栏可见", window.sidebar.isVisible(), ""),
                ("工具栏在首页隐藏", not window.toolbar.isVisible(), ""),
                ("模块面板在首页隐藏", not window.palette_panel.isVisible(), ""),
                ("标题栏横跨整个窗口宽度（不被面板挤在中间）",
                 window.title_bar.width() >= window.width() - 4,
                 f"titlebar={window.title_bar.width()} window={window.width()}"),
                (
                    "停止按钮在首页可见（不然跑起来就停不掉）",
                    window.action_stop.isVisible(),
                    "",
                ),
                ("侧边栏底部只显示流程数", window.sidebar.footer.text().endswith("条流程")
                 and "插件" not in window.sidebar.footer.text(),
                 window.sidebar.footer.text()),
                ("状态栏整条已移除", window.findChild(QStatusBar) is None, ""),
                ("没有未保存改动", not window._dirty, str(window._dirty)),
            ]
            window.confirm_on_close = False
            window.close()

        for name, ok, detail in checks:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}"
                  + (f"\n        {detail}" if not ok and detail else ""))
            failures += 0 if ok else 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return failures


def scenario_editor() -> int:
    """带流程参数启动：应该直接在编辑器里打开它。"""
    from host.main_window import MODE_EDITOR

    tmpdir = Path(tempfile.mkdtemp(prefix="myautowork-launch-editor-"))
    failures = 0
    try:
        demo = REPO_ROOT / "workflows" / "demo.json"
        code, created = _run_main(["--no-history", "--workdir", str(tmpdir), str(demo)])
        checks: list[tuple[str, bool, str]] = [("main() 正常返回 0", code == 0, str(code))]

        if not created:
            checks.append(("创建了主窗口", False, "没有创建"))
        else:
            window = created[0]
            checks += [
                ("直接进了编辑器", window.mode == MODE_EDITOR, window.mode),
                ("画布铺出了 4 个节点", len(window.scene.node_items) == 4,
                 str(sorted(window.scene.node_items))),
                ("画布铺出了 4 条连线", len(window.scene.edge_items) == 4,
                 str(len(window.scene.edge_items))),
                ("侧边栏收起", not window.sidebar.isVisible(), ""),
                ("工具栏出现", window.toolbar.isVisible(), ""),
                ("模块面板在编辑器里可见", window.palette_panel.isVisible(), ""),
                ("窗口标题是文件名", "demo.json" in window.windowTitle(), window.windowTitle()),
            ]
            window.confirm_on_close = False
            window.close()

        for name, ok, detail in checks:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}"
                  + (f"\n        {detail}" if not ok and detail else ""))
            failures += 0 if ok else 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return failures


def main() -> int:
    scenario = sys.argv[1] if len(sys.argv) > 1 else "list"
    print(f"[场景：{scenario}]")
    failures = scenario_editor() if scenario == "editor" else scenario_list()
    print(f"\n失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
