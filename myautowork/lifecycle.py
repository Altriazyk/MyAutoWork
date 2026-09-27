"""插件级的清理钩子。

**为什么需要这个。** 插件会开文件句柄、起子进程、占端口、写临时文件 —— 而这些都是
**进程外的资源**。插件进程被 kill 的时候，操作系统只回收内存和句柄，**不会**
去终止它起过的子进程、不会删它写了一半的临时文件、不会释放它占着的设备。

结果是：插件看起来停了，但那个子进程还在跑、那个文件还锁着、那个端口还占着。
下一次启动同一个插件就会撞上"文件被占用""端口已被监听"这类错误，而现场已经没了。

**这是从 Cordis 抄来的一个想法**（它的 ``ctx.effect()``：注册即副作用，卸载时逆序撤销）。
Cordis 用它做热插拔，这里用它做**干净地停下来**。

**注册表是插件级的，不是上下文级的。** ``Context`` 每次动作调用都会新建一个，
钩子挂在它上面的话，动作一返回就跟着没了 —— 而"我起了一个子进程"这件事要一直
活到插件被卸载。所以存在模块级的列表里，插件进程退出前统一跑。

**什么时候该用，什么时候不该用。**

该用：你占了一个**插件进程之外**的东西，而它不会因为进程消失而消失 ——
全局热键注册、常驻的文件监视器、监听中的端口、你自己起的子进程。

**不该用**：那个东西本来就该活过插件。典型的是 ``core.app`` 的「启动程序」——
它起的是用户要用的模拟器/编辑器，``DETACHED_PROCESS`` 就是为了让它活过流程。
给它登记 terminate 会变成"用户关掉本软件时，那些程序被一起杀掉"。
真要"跑完就关"，那是流程自己的事：接一个「关闭程序」节点，
让"关"这一步**在流程上看得见**，而不是藏进插件卸载里。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

__all__ = ["clear_cleanups", "on_cleanup", "pending_cleanups", "run_cleanups"]

#: 插件进程内的清理回调。**后注册的先执行**（像栈一样）—— 先起的服务后停。
_CLEANUPS: list[tuple[str, Callable[[], Any]]] = []
_LOCK = threading.Lock()

#: 单个回调最多跑多久。清理卡住比不清理更糟：它会让插件永远停不下来。
DEFAULT_TIMEOUT = 1.5

#: 所有回调加起来最多跑多久。**必须有个总数** —— 回调是串行的，每个 1.5 秒的话
#: 十个就是 15 秒，早就超过内核愿意等的时长了，结果就是"清理还没跑完就被硬杀"，
#: 而那和没有清理是一样的。
DEFAULT_BUDGET = 3.5


def run_cleanups(
    *, timeout: float = DEFAULT_TIMEOUT, budget: float = DEFAULT_BUDGET
) -> list[str]:
    """反序执行所有清理钩子。返回**出问题**的描述列表（正常就是空列表）。

    **每个回调单独限时，整体还有预算。** 一个卡死的清理函数不能拖住后面所有的 ——
    每个都放进自己的线程 join 一个上限。但"每个都不超时"不等于"总共不超时"，
    所以还要一个总预算：用完了剩下的直接跳过并记一笔，不能让插件停不下来。

    **出错不中断。** 一个清理失败不该让剩下的不跑 —— 那等于"一个资源没收拾干净，
    于是所有资源都不收拾了"。
    """
    with _LOCK:
        items = list(reversed(_CLEANUPS))
        _CLEANUPS.clear()

    problems: list[str] = []
    deadline = time.monotonic() + max(0.0, budget)

    for label, callback in items:
        left = deadline - time.monotonic()
        if left <= 0.05:
            problems.append(f"{label}：清理预算用完了，没来得及跑")
            continue

        box: list[str] = []

        def run(cb: Callable[[], Any] = callback, sink: list[str] = box) -> None:
            try:
                result = cb()
                # 回调返回可调用对象时，那才是真正的清理动作（Cordis 的 disposer 写法）。
                if callable(result):
                    result()
            except Exception as exc:  # noqa: BLE001 - 清理失败什么类型都可能
                sink.append(f"{type(exc).__name__}: {exc}")

        worker = threading.Thread(target=run, name=f"cleanup-{label}", daemon=True)
        worker.start()
        worker.join(min(timeout, left))
        if worker.is_alive():
            # 线程是 daemon，主进程退出时会被带走 —— 这里只能记一笔，不能等它。
            problems.append(f"{label}：超过 {min(timeout, left):g} 秒还没结束，先放着继续退")
        elif box:
            problems.append(f"{label}：{box[0]}")

    return problems


def on_cleanup(callback: Callable[[], Any], *, name: str = "") -> None:
    """登记一个"插件被卸载时要做的事"。

    回调可以返回一个可调用对象（disposer），那它会被当作真正的清理动作 ——
    这样写法可以很自然：

        proc = subprocess.Popen(...)
        ctx.on_cleanup(lambda: proc.terminate())

    也可以直接传一个清理函数，两种都行。
    """
    if not callable(callback):
        raise TypeError(f"清理钩子必须是可调用的，收到 {type(callback).__name__}")
    label = name or getattr(callback, "__name__", "") or "匿名清理"
    with _LOCK:
        _CLEANUPS.append((label, callback))


def pending_cleanups() -> int:
    """登记了几个清理钩子。给测试和自检用。"""
    with _LOCK:
        return len(_CLEANUPS)


def clear_cleanups() -> None:
    """丢掉所有登记（不执行）。给测试的隔离用。"""
    with _LOCK:
        _CLEANUPS.clear()
