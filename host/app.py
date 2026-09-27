"""myautowork 的图形入口。

    python -m host
    python -m host workflows/demo.json      # 直接进编辑器打开某条流程

启动流程刻意做得简单：建 registry（只读 manifest）→ 建窗口（这时才按需拉起插件进程）
→ 停在**流程列表**首页。启动速度不取决于装了多少插件，也不取决于有多少条流程 ——
首页只读工作流文件的头部信息。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # 支持从任意目录运行
    sys.path.insert(0, str(REPO_ROOT))

# DPI 感知必须赶在 QApplication 之前：Qt 会在建 QApplication 时尝试设置，之后再想改
# 系统就不允许了。开了之后在缩放屏上文字是清晰的（不开的话 Windows 会把整个窗口位图
# 拉伸，糊），更重要的是拾取器拿到的 UIA 坐标和屏幕截图会在同一个坐标系里。
from myautowork import dpi as _dpi  # noqa: E402

_DPI_MODE = _dpi.enable()

from PySide6.QtWidgets import QApplication  # noqa: E402

from kernel.registry import Registry  # noqa: E402
from kernel.store import RunStore  # noqa: E402

from . import theme  # noqa: E402
from .main_window import APP_NAME, MainWindow  # noqa: E402

DEFAULT_PLUGINS_DIR = REPO_ROOT / "plugins"
DEFAULT_HISTORY = REPO_ROOT / "history.db"
DEFAULT_WORKFLOWS_DIR = REPO_ROOT / "workflows"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{APP_NAME}-studio",
        description=f"{APP_NAME} 图形界面",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            f"  python -m host                                  # 打开流程列表\n"
            f"  python -m host workflows/demo.json              # 直接编辑某条流程\n"
            f"  python -m host --workflows-dir D:\\\\bots           # 换一个流程目录\n"
        ),
    )
    parser.add_argument("workflow", nargs="?", help="启动时在编辑器里打开的工作流 JSON")
    parser.add_argument("--plugins-dir", type=Path, default=DEFAULT_PLUGINS_DIR, help="插件目录")
    parser.add_argument(
        "--workflows-dir", type=Path, default=DEFAULT_WORKFLOWS_DIR, help="流程目录（首页扫描它）"
    )
    parser.add_argument("--workdir", type=Path, default=None, help="运行时工作目录，默认当前目录")
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY, help="运行历史库路径")
    parser.add_argument("--no-history", action="store_true", help="不写运行历史")
    parser.add_argument("--timeout", type=float, default=300.0, help="单个节点的超时秒数")
    parser.add_argument("--python", default=None, help="启动插件进程用的解释器")
    return parser


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    # 只把程序名交给 Qt，避免它去解析我们自己的参数。
    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    theme.apply_theme(app)

    registry = Registry(
        args.plugins_dir,
        repo_root=REPO_ROOT,
        python_exe=args.python,
        worker_timeout=args.timeout,
    )
    store = None if args.no_history else RunStore(args.history)

    window = MainWindow(
        registry,
        store=store,
        workdir=args.workdir or Path.cwd(),
        workflows_dir=args.workflows_dir,
        plugins_dir=args.plugins_dir,
        timeout=args.timeout,
    )
    # 插件进程的 stderr 在 RPC 读取线程里产生，必须经信号回主线程才能进日志面板。
    registry.on_worker_stderr = window.workerStderr.emit

    window.show()

    # 指定了流程才进编辑器；否则停在首页。首页本来就是"发射台"。
    if args.workflow:
        if window.load_path(Path(args.workflow)):
            window.show_editor()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
