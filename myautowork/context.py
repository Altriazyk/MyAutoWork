"""插件运行时拿到的上下文对象。

规则很简单：**插件需要的一切都从 ``ctx`` 拿**，不要自己去碰全局状态。

这样做不是为了安全（我们已经假定插件是安全代码），而是为了三件很实际的事：

1. 日志能自动带上"哪个节点、哪次运行"，否则多任务并发时日志是一团浆糊
2. 进度能实时推回界面，用户知道脚本没死
3. 运行历史能被完整记录下来，事后能查

界面自动化尤其依赖第 2 点 —— "等待元素出现"可能卡十几秒，没有进度提示，
用户会以为程序死了然后去点关闭。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping

__all__ = ["Context", "LogSink"]

LogSink = Callable[[str, str, dict[str, Any] | None], None]


def _null_sink(level: str, message: str, data: dict[str, Any] | None) -> None:  # pragma: no cover
    pass


class Context:
    """传给每个动作/触发器的运行时上下文。"""

    __slots__ = (
        "plugin_id",
        "action_id",
        "node_id",
        "run_id",
        "variables",
        "workdir",
        "artifacts_dir",
        "_sink",
    )

    def __init__(
        self,
        *,
        plugin_id: str = "",
        action_id: str = "",
        node_id: str = "",
        run_id: str = "",
        variables: Mapping[str, Any] | None = None,
        workdir: str | os.PathLike[str] | None = None,
        artifacts_dir: str | os.PathLike[str] | None = None,
        sink: LogSink | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.action_id = action_id
        self.node_id = node_id
        self.run_id = run_id
        self.variables: MutableMapping[str, Any] = dict(variables or {})
        self.workdir = Path(workdir) if workdir is not None else Path.cwd()
        self.artifacts_dir = (
            Path(artifacts_dir) if artifacts_dir is not None else self.workdir / "artifacts"
        )
        self._sink: LogSink = sink or _null_sink

    # -- 日志 -----------------------------------------------------------------

    def log(self, message: str, level: str = "info", **data: Any) -> None:
        self._sink(level, str(message), data or None)

    def debug(self, message: str, **data: Any) -> None:
        self.log(message, "debug", **data)

    def info(self, message: str, **data: Any) -> None:
        self.log(message, "info", **data)

    def warning(self, message: str, **data: Any) -> None:
        self.log(message, "warning", **data)

    warn = warning

    def error(self, message: str, **data: Any) -> None:
        self.log(message, "error", **data)

    def progress(self, value: float, message: str = "") -> None:
        """上报 0~1 的进度，界面上会显示成节点上的进度条。"""
        self._sink(
            "progress",
            message,
            {"progress": max(0.0, min(1.0, float(value)))},
        )

    # -- 变量 -----------------------------------------------------------------

    def get_var(self, name: str, default: Any = None) -> Any:
        return self.variables.get(name, default)

    def set_var(self, name: str, value: Any) -> None:
        """写入工作流级变量。

        会通过 RPC 同步回内核，所以后续节点和表达式都能读到。
        """
        self.variables[name] = value
        self._sink("set_var", name, {"value": value})

    @property
    def vars(self) -> MutableMapping[str, Any]:
        return self.variables

    # -- 文件 -----------------------------------------------------------------

    def resolve(self, path: str | os.PathLike[str]) -> Path:
        """把相对路径解析到工作目录下，避免插件各自乱猜 cwd。"""
        p = Path(path)
        return p if p.is_absolute() else (self.workdir / p)

    def artifact(self, name: str) -> Path:
        """给插件存放截图、导出文件等的目录，会自动创建。

        界面自动化失败时保存的截图就放这里，由内核负责和运行历史关联起来。
        """
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        return self.artifacts_dir / name

    # -- 收尾 -----------------------------------------------------------------

    def on_cleanup(self, callback: Callable[[], Any], *, name: str = "") -> None:
        """登记一件"插件被卸载时要做的事"。

        **插件起的子进程、开的文件句柄，内核是收不掉的** —— kill 只回收内存和句柄，
        不会去终止你起过的子进程，也不会删你写了一半的文件。所以你自己登记：

            proc = subprocess.Popen(...)
            ctx.on_cleanup(lambda: proc.terminate())

        内核停插件时会**先给它一个机会自己收尾，再动手杀**。

        注意 ``ctx`` 是每次动作调用新建的，但这个登记**活在插件进程上** ——
        动作返回之后它还在，一直留到插件被卸载。
        """
        from .lifecycle import on_cleanup as _register  # noqa: PLC0415 - 避免循环导入

        label = name or f"{self.plugin_id}/{self.action_id}"
        _register(callback, name=label)
