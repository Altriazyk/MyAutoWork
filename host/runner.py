"""在后台线程里跑工作流。

为什么要开线程：一次自动化运行可能持续几分钟（界面上要等窗口出现、要等数据加载）。
放在主线程里，Qt 的事件循环会被完全堵死，界面变灰、进度不动、连"停止"都点不了。

事件靠 Qt 信号回主线程。跨线程的信号连接默认是队列式的，所以槽函数一定在主线程里
执行 —— 可以安全地碰控件。这条规则别破，破了就是随机崩溃。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from kernel.engine import Engine, RunResult
from kernel.graph import Workflow

__all__ = ["WorkflowRunThread"]


class WorkflowRunThread(QThread):
    """跑一次工作流，把引擎事件转发成 Qt 信号。"""

    eventOccurred = Signal(str, object)
    runFinished = Signal(object)

    def __init__(
        self,
        registry: Any,
        store: Any,
        workflow: Workflow,
        *,
        workdir: Path,
        timeout: float = 300.0,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.workflow = workflow
        self.workdir = Path(workdir)
        # 引擎在主线程构造，run() 在子线程执行。on_event 里只发信号，不碰控件。
        self.engine = Engine(
            registry,
            store=store,
            default_timeout=timeout,
            workdir=self.workdir,
            on_event=self._forward,
        )

    def _forward(self, kind: str, payload: dict[str, Any]) -> None:
        self.eventOccurred.emit(kind, payload)

    def run(self) -> None:  # noqa: D102 - QThread 重写
        result: RunResult | None = None
        try:
            result = self.engine.run(self.workflow, workdir=self.workdir)
        except Exception as exc:  # pragma: no cover - 引擎自己会兜住，这里是最后一道
            self.eventOccurred.emit(
                "log",
                {"node_id": "", "level": "error", "message": f"运行时异常：{type(exc).__name__}: {exc}"},
            )
        self.runFinished.emit(result)

    def cancel(self) -> None:
        """请求停止当前节点跑完就停。

        不强杀插件进程：一个正在点鼠标的动作被半路掐掉，可能把目标程序留在
        半操作状态，比多等几秒糟得多。而且实测下来"杀了进程就能立刻停住"这个前提
        本身就不成立 —— 详见 ``Engine.cancel()`` 的说明。
        """
        self.engine.cancel()
