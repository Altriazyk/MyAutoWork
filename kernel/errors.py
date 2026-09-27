"""内核异常。

分得细是为了让界面能给出不同的处理方式：契约错误应该阻止运行并指向插件，
运行错误应该高亮出错的节点，超时应该提示"插件无响应、已终止"。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MyAutoWorkError",
    "ManifestError",
    "PluginError",
    "WorkerTimeout",
    "GraphError",
    "ExpressionError",
    "WorkflowError",
    "RunCancelled",
]


class MyAutoWorkError(Exception):
    """所有内核异常的基类。"""


class ManifestError(MyAutoWorkError):
    """manifest.json 缺失、格式错误或与代码声明不一致。"""


class PluginError(MyAutoWorkError):
    """插件进程返回了错误。"""

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


class WorkerTimeout(PluginError):
    """插件在超时时间内没有响应，进程已被强制终止。"""


class GraphError(MyAutoWorkError):
    """工作流结构非法（缺节点、端口不存在、类型不匹配等）。"""


class ExpressionError(MyAutoWorkError):
    """表达式求值失败，例如引用了还不存在的上游输出。"""


class WorkflowError(MyAutoWorkError):
    """工作流运行期失败。"""


class RunCancelled(MyAutoWorkError):
    """用户请求停止。

    节点是不可中断的（正在点鼠标的动作没法安全地半路掐掉），所以取消的语义是
    "当前节点跑完就停"。这比强杀进程安全：强杀可能把目标程序留在半操作状态。
    """
