"""插件侧运行时。用 ``python -m kernel.worker --plugin-dir <目录>`` 启动。

它做三件事：

1. 把 **stdout 让给协议**，插件里的 ``print`` 自动变成 stderr 日志
2. 加载插件入口模块，读取装饰器注册出来的接口
3. 循环处理内核发来的 ``describe`` / ``invoke`` / ``ping`` / ``shutdown``

第 1 点看着琐碎，其实很关键：插件作者一定会写 print。如果不做重定向，一次
print 就会往协议里塞一行非 JSON，内核要么报错要么静默丢弃，排查起来非常痛苦。
直接把 fd 1 换成 stderr，问题在源头消失。
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping

# --------------------------------------------------------------------------- #
# 协议通道
# --------------------------------------------------------------------------- #

#: 真正的协议输出流。默认指向原始 stdout，setup_streams() 会替换它。
_OUT: Any = sys.stdout
_IN: Any = sys.stdin


def setup_streams() -> None:
    """把 fd 1 让给协议，其余一切都走 stderr。"""
    global _OUT, _IN

    raw_out_fd = os.dup(1)
    # 之后所有写 fd 1 的操作（包括 C 扩展里的 printf）都会落到 stderr。
    sys.stdout = sys.stderr
    os.dup2(2, 1)

    _OUT = os.fdopen(raw_out_fd, "w", encoding="utf-8", buffering=1, newline="\n")
    _IN = io.TextIOWrapper(
        os.fdopen(os.dup(0), "rb"), encoding="utf-8", errors="replace", newline="\n"
    )


def send(message: Mapping[str, Any]) -> None:
    try:
        _OUT.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
        _OUT.flush()
    except (BrokenPipeError, OSError, ValueError):
        # 内核没了，插件跟着退。
        raise SystemExit(0)


def reply(request_id: Any, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def reply_error(request_id: Any, *, message: str, kind: str = "plugin_error", **extra: Any) -> None:
    payload = {"kind": kind, "message": message}
    payload.update(extra)
    send({"jsonrpc": "2.0", "id": request_id, "error": payload})


def notify(params: Mapping[str, Any]) -> None:
    send({"jsonrpc": "2.0", "method": "notify", "params": dict(params)})


# --------------------------------------------------------------------------- #
# 插件加载
# --------------------------------------------------------------------------- #


def load_plugin(plugin_dir: Path, entry: str) -> Any:
    """按路径加载插件入口模块。

    不用 import 语句，是因为插件目录名形如 ``core.file.write``，带点号，不是合法
    的 Python 包名。按文件路径加载顺便让插件可以随意命名自己的目录。
    """
    entry_path = plugin_dir / entry
    if not entry_path.is_file():
        raise FileNotFoundError(f"插件入口不存在：{entry_path}")

    # 让插件能 import 自己的同级模块。
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))

    module_name = "myautowork_plugin_entry"
    spec = importlib.util.spec_from_file_location(module_name, entry_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {entry_path} 创建模块规格")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 调用处理
# --------------------------------------------------------------------------- #


class WorkerRuntime:
    def __init__(self, plugin_dir: Path, entry: str) -> None:
        self.plugin_dir = plugin_dir
        self.entry = entry
        self.plugin_module: Any = None
        self.actions: dict[str, Any] = {}
        self.triggers: dict[str, Any] = {}
        self.plugin_id = plugin_dir.name

    def load(self) -> None:
        from myautowork import registered_actions, registered_triggers
        from myautowork.decorators import clear_registry

        clear_registry()
        self.plugin_module = load_plugin(self.plugin_dir, self.entry)
        self.actions = registered_actions()
        self.triggers = registered_triggers()

        if not self.actions and not self.triggers:
            raise RuntimeError(
                "插件没有注册任何 action 或 trigger。"
                "确认入口模块里用了 @action / @trigger 装饰器。"
            )

    def describe(self) -> dict[str, Any]:
        from myautowork import describe_interface

        return {
            "plugin_id": self.plugin_id,
            "directory": str(self.plugin_dir),
            **describe_interface(),
        }

    def invoke(self, params: Mapping[str, Any]) -> dict[str, Any]:
        from myautowork import NOTSET, Context, coerce
        from myautowork.fields import Field

        action_id = str(params.get("action_id", ""))
        spec = self.actions.get(action_id) or self.triggers.get(action_id)
        if spec is None:
            raise KeyError(
                f"插件 {self.plugin_id} 没有名为 {action_id!r} 的动作"
                f"（可用：{sorted(set(self.actions) | set(self.triggers))}）"
            )

        node_id = str(params.get("node_id", ""))
        run_id = str(params.get("run_id", ""))
        raw_args: dict[str, Any] = dict(params.get("params") or {})
        variables: dict[str, Any] = dict(params.get("variables") or {})
        workdir = Path(str(params.get("workdir") or os.getcwd()))
        artifacts_dir = Path(str(params.get("artifacts_dir") or (workdir / "artifacts")))

        def sink(level: str, message: str, data: dict[str, Any] | None) -> None:
            if level == "set_var":
                variables[message] = (data or {}).get("value")
                return
            payload: dict[str, Any] = {
                "node_id": node_id,
                "level": level,
                "message": message,
            }
            if data:
                payload.update(data)
            notify(payload)

        ctx = Context(
            plugin_id=self.plugin_id,
            action_id=action_id,
            node_id=node_id,
            run_id=run_id,
            variables=variables,
            workdir=workdir,
            artifacts_dir=artifacts_dir,
            sink=sink,
        )

        # 组装入参：按声明取值 → 缺的用默认值。
        kwargs: dict[str, Any] = {}
        missing: list[str] = []
        for name, field_spec in spec.inputs.items():
            assert isinstance(field_spec, Field)
            if name in raw_args and raw_args[name] is not None:
                kwargs[name] = coerce(raw_args[name], field_spec.kind)
            elif field_spec.default is not NOTSET:
                kwargs[name] = field_spec.default
            elif field_spec.required:
                missing.append(name)
        if missing:
            raise ValueError(f"缺少必填输入：{', '.join(missing)}")

        extra = set(raw_args) - set(spec.inputs)
        if extra:
            ctx.warning(f"忽略了未声明的输入：{', '.join(sorted(extra))}")

        started = time.perf_counter()
        result = spec.func(ctx, **kwargs)
        duration_ms = int((time.perf_counter() - started) * 1000)

        # 规整输出：必须是对象，否则下游按端口取值的逻辑就无从下手。
        output: dict[str, Any]
        if result is None:
            output = {}
        elif isinstance(result, Mapping):
            output = dict(result)
        else:
            ctx.warning(
                f"动作返回了 {type(result).__name__}，已包装为 {{'result': ...}}。"
                "插件的返回值应该是 dict，键名与 outputs 声明一致。"
            )
            output = {"result": result}

        # 按声明的输出类型做一次温和转换，让类型系统真正起作用。
        for name, field_spec in spec.outputs.items():
            if name in output:
                output[name] = coerce(output[name], field_spec.kind)

        undeclared = set(output) - set(spec.outputs)
        if undeclared:
            ctx.warning(
                f"返回了未声明的输出：{', '.join(sorted(undeclared))}。"
                "下游在画布上连不到这些端口，请补进 outputs 声明。"
            )

        return {
            "ok": True,
            "output": output,
            "duration_ms": duration_ms,
            "variables": variables,
        }


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #


def _is_expected(exc: BaseException) -> bool:
    """这个异常是不是插件**主动**抛的"预期之内"？

    延迟 import：worker 跑在插件自己的解释器里，正常情况 SDK 一定在，但万一不在，
    也不该为了判断一个异常把 worker 弄挂。
    """
    try:
        from myautowork.errors import ExpectedError  # noqa: PLC0415
    except Exception:  # pragma: no cover - 环境异常时按"真有 bug"处理，宁可多打栈
        return False
    return isinstance(exc, ExpectedError)


def _handle(runtime: WorkerRuntime, message: Mapping[str, Any]) -> bool:
    """处理一条请求。返回 False 表示该退出了。"""
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if method == "shutdown":
        # **先让插件自己收拾，再退出。** 它起的子进程、开的句柄、占的端口，
        # 内核是收不掉的（kill 只回收内存和句柄）。所以给它一个机会跑清理钩子。
        #
        # 必须在**回 reply 之前**做完：回了 reply 内核就认为它停了，可能立刻
        # 去启下一个插件，而那时旧插件还占着资源。
        from myautowork import run_cleanups  # noqa: PLC0415 - worker 里延迟导入

        problems = run_cleanups()
        for line in problems:
            notify({"node_id": "", "level": "warning", "message": f"清理时出问题 —— {line}"})
        reply(request_id, {"ok": True, "cleanups": len(problems)})
        return False

    if method == "ping":
        reply(
            request_id,
            {
                "ok": True,
                "plugin_id": runtime.plugin_id,
                "actions": sorted(runtime.actions),
                "triggers": sorted(runtime.triggers),
            },
        )
        return True

    if method == "describe":
        reply(request_id, runtime.describe())
        return True

    if method == "invoke":
        try:
            reply(request_id, runtime.invoke(params))
        except Exception as exc:
            # 完整栈打到 stderr（内核会转发到运行日志），
            # 给内核的只有精简信息，避免日志被几十行栈刷屏。
            #
            # **但"预期之内的错"不打栈。** 条件不成立、参数没填对这类是流程的一部分，
            # 用户天天碰到；为它们打一屏栈，等于把"if 走了 false 分支"渲染成一次故障。
            # 判定全靠插件抛 ExpectedError 这个显式标记（见 myautowork/errors.py）。
            if not _is_expected(exc):
                traceback.print_exc()
            else:
                print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            reply_error(
                request_id,
                message=f"{type(exc).__name__}: {exc}",
                kind="action_error",
                action_id=str(params.get("action_id", "")),
                node_id=str(params.get("node_id", "")),
                traceback="".join(traceback.format_exc().splitlines(keepends=True)[-6:]),
            )
        return True

    reply_error(request_id, message=f"未知的 RPC 方法：{method!r}", kind="protocol_error")
    return True


def main(argv: list[str] | None = None) -> int:
    # 必须在任何窗口/COM 之前开 DPI 感知：不开的话 UIA 坐标和屏幕截图会差一个缩放比
    # （实测 125% 缩放下是 1.25 倍），图像匹配会自信地找到完全错误的位置。
    from myautowork import dpi  # noqa: PLC0415 - 延迟到入口，插件自己不用关心

    dpi.enable()

    parser = argparse.ArgumentParser(prog="kernel.worker", description="myautowork 插件运行时")
    parser.add_argument("--plugin-dir", required=True, help="插件目录")
    parser.add_argument("--entry", default="main.py", help="入口文件名（覆盖 manifest）")
    args = parser.parse_args(argv)

    setup_streams()

    plugin_dir = Path(args.plugin_dir).resolve()
    runtime = WorkerRuntime(plugin_dir, args.entry)
    load_error: str | None = None
    try:
        runtime.load()
    except Exception as exc:
        traceback.print_exc()
        load_error = f"插件加载失败：{type(exc).__name__}: {exc}"

    for raw in _IN:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print(f"[worker] 忽略非法 JSON 请求：{line[:200]}", file=sys.stderr)
            continue
        if not isinstance(message, Mapping):
            continue

        # 加载失败时进程**不退出**，而是把原因明确回给每一个请求。
        # 直接崩掉的话，内核只能看到"进程意外退出（退出码 2）"，
        # 真正的原因（哪个动作没注册、语法错在哪）就丢了。
        if load_error is not None and message.get("method") != "shutdown":
            reply_error(message.get("id"), message=load_error, kind="load_error")
            continue

        try:
            keep_going = _handle(runtime, message)
        except SystemExit:
            raise
        except Exception as exc:  # pragma: no cover - 兜底
            traceback.print_exc()
            reply_error(
                message.get("id"),
                message=f"worker 内部错误：{type(exc).__name__}: {exc}",
                kind="worker_error",
            )
            keep_going = True
        if not keep_going:
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
