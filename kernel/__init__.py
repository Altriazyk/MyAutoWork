"""myautowork 内核。

职责边界（很重要，别让任何一层越界）：

- ``manifest``  静态描述：不执行插件代码就能知道它是谁
- ``registry``  发现插件、按需拉起 worker 进程、缓存接口描述
- ``rpc``       内核侧 RPC 客户端：stdio JSON-RPC + 故障处理
- ``worker``    插件侧运行时：加载插件、执行动作、把日志推回来
- ``graph``     工作流数据模型 + 静态校验
- ``expr``      表达式求值（常量 / 变量 / 上游输出）
- ``engine``    执行流 + 数据流双线执行引擎
- ``store``     运行历史持久化
- ``library``   工作流库：扫描目录 + 关联最近一次运行

界面（host）只依赖 ``registry`` / ``graph`` / ``engine`` / ``store`` / ``library``，
不直接碰 ``subprocess`` 和 JSON-RPC。
"""

from __future__ import annotations

from .errors import (
    ExpressionError,
    GraphError,
    ManifestError,
    MyAutoWorkError,
    PluginError,
    RunCancelled,
    WorkerTimeout,
    WorkflowError,
)
from .manifest import API_VERSION, Manifest, load_manifest

__version__ = "0.1.0"

__all__ = [
    "API_VERSION",
    "__version__",
    "Manifest",
    "load_manifest",
    "MyAutoWorkError",
    "ManifestError",
    "PluginError",
    "WorkerTimeout",
    "GraphError",
    "ExpressionError",
    "WorkflowError",
    "RunCancelled",
]
