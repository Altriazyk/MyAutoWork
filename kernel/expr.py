"""表达式求值：把参数声明变成真实的值。

三种取值来源，对应属性面板上的三种模式：

============  ==========================================  ============================
mode          含义                                        典型场景
============  ==========================================  ============================
``literal``   照原样用                                    超时 10 秒
``expr``      求值 ``{{ ... }}``                          路径里插日期、引用远处的输出
``upstream``  取连到该输入的数据边的值                    上一步读出来的文本喂给写文件
============  ==========================================  ============================

``expr`` 是这套设计里性价比最高的一块：只要节点执行过，它的输出就缓存进
``$node.<id>.<port>``，**任何后续节点都能直接引用**，不必拉一根横跨半个画布的
长线。画布因此能保持干净，跨分支取数据也不成问题。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .errors import ExpressionError
from .graph import Node, ParamValue, Workflow

__all__ = ["EvalContext", "make_builtins", "interpolate", "resolve_param", "resolve_inputs", "lookup"]

_EXPR_PATTERN = re.compile(r"\{\{(.+?)\}\}", re.DOTALL)


@dataclass
class EvalContext:
    """一次运行期间的求值环境。"""

    run_id: str
    workflow: Workflow
    workdir: Path
    variables: dict[str, Any] = dc_field(default_factory=dict)
    #: node_id -> {output_port: value}，节点执行后写入。
    cache: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    builtins: dict[str, Any] = dc_field(default_factory=dict)


def make_builtins(
    *,
    run_id: str,
    workflow: Workflow,
    workdir: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """内置变量。``$date`` / ``$now`` 这类在写路径、命名文件时天天要用。"""
    moment = now or datetime.now()
    return {
        "now": moment.isoformat(timespec="seconds"),
        "date": moment.strftime("%Y-%m-%d"),
        "time": moment.strftime("%H:%M:%S"),
        "datetime": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": int(moment.timestamp()),
        "run_id": run_id,
        "workflow": workflow.name,
        "workflow_id": workflow.id,
        "workdir": str(workdir),
    }


# --------------------------------------------------------------------------- #
# 路径查找
# --------------------------------------------------------------------------- #


def lookup(path: str, ctx: EvalContext) -> Any:
    """解析一个点分路径，例如 ``$node.n2.slept``、``$vars.token``、``$date``。"""
    parts = [p for p in path.strip().split(".") if p]
    if not parts:
        raise ExpressionError("表达式为空")

    head = parts[0]

    if head in ("$node", "node"):
        if len(parts) < 2:
            raise ExpressionError("$node 后面要跟节点 id，例如 {{ $node.n2.text }}")
        node_id = parts[1]
        outputs = ctx.cache.get(node_id)
        if outputs is None:
            available = ", ".join(sorted(ctx.cache)) or "（还没有节点执行过）"
            raise ExpressionError(
                f"节点 {node_id!r} 还没有执行，取不到它的输出。"
                f"常见原因：数据线连到了执行顺序更早的节点 —— 数据必须先于使用就绪。"
                f"已执行过的节点：{available}"
            )
        if len(parts) == 2:
            return outputs
        port = parts[2]
        if port not in outputs:
            raise ExpressionError(
                f"节点 {node_id!r} 没有输出端口 {port!r}（它有：{sorted(outputs) or '无'}）"
            )
        value = outputs[port]
        for extra in parts[3:]:
            value = _index(value, extra, path)
        return value

    if head in ("$vars", "vars"):
        if len(parts) < 2:
            available = ", ".join(sorted(ctx.variables)) or "（无）"
            raise ExpressionError(f"$vars 后面要跟变量名。当前变量：{available}")
        name = parts[1]
        if name not in ctx.variables:
            available = ", ".join(sorted(ctx.variables)) or "（无）"
            raise ExpressionError(f"变量 {name!r} 不存在。当前变量：{available}")
        value = ctx.variables[name]
        for extra in parts[2:]:
            value = _index(value, extra, path)
        return value

    key = head.lstrip("$")
    if key in ctx.variables:
        value = ctx.variables[key]
        for extra in parts[1:]:
            value = _index(value, extra, path)
        return value
    if key in ctx.builtins:
        return ctx.builtins[key]

    available_vars = ", ".join(f"${n}" for n in sorted(ctx.variables)) or "（无）"
    available_builtins = ", ".join(f"${n}" for n in sorted(ctx.builtins))
    raise ExpressionError(
        f"无法识别的名字 {head!r}。可用的内置变量：{available_builtins}；"
        f"工作流变量：{available_vars}；节点输出请用 $node.<节点id>.<端口>"
    )


def _index(value: Any, key: str, path: str) -> Any:
    """支持 ``$node.n2.items.0`` 这种对 list/dict 的下钻。"""
    if isinstance(value, Mapping):
        if key not in value:
            raise ExpressionError(f"{path} 中的 {key!r} 不存在（可用：{sorted(value)}）")
        return value[key]
    if isinstance(value, (list, tuple)):
        try:
            return value[int(key)]
        except (ValueError, IndexError) as exc:
            raise ExpressionError(f"{path} 中的下标 {key!r} 无效") from exc
    raise ExpressionError(f"{path} 中的 {key!r} 无法从 {type(value).__name__} 取值")


# --------------------------------------------------------------------------- #
# 插值
# --------------------------------------------------------------------------- #


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def interpolate(text: str, ctx: EvalContext) -> Any:
    """替换文本里的 ``{{ ... }}``。

    一个关键细节：如果整段文本**恰好就是一个** ``{{ ... }}``，返回原始对象而不是
    字符串。这样 ``{{ $node.n2.count }}`` 传给数字输入仍然是数字，不会被转成
    ``"3"`` 之后在插件里炸掉。
    """
    if not isinstance(text, str) or "{{" not in text:
        return text

    matches = list(_EXPR_PATTERN.finditer(text))
    if not matches:
        return text

    if len(matches) == 1 and matches[0].span() == (0, len(text)):
        return lookup(matches[0].group(1), ctx)

    chunks: list[str] = []
    cursor = 0
    for match in matches:
        chunks.append(text[cursor : match.start()])
        chunks.append(_stringify(lookup(match.group(1), ctx)))
        cursor = match.end()
    chunks.append(text[cursor:])
    return "".join(chunks)


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #


def resolve_param(param: ParamValue, ctx: EvalContext) -> Any:
    """把一个参数值解析成实际值。``upstream`` 由 resolve_inputs 处理。"""
    if param.mode == "literal":
        return param.value
    if param.mode == "expr":
        return interpolate("" if param.value is None else str(param.value), ctx)
    raise ExpressionError("upstream 参数必须由数据边提供，不能单独求值")


def _value_from_edge(edge: Any, ctx: EvalContext) -> Any:
    outputs = ctx.cache.get(edge.src)
    if outputs is None:
        raise ExpressionError(
            f"数据边 {edge.src}.{edge.src_port} → {edge.dst}.{edge.dst_port} 的源节点还没有执行。"
            "检查一下执行顺序：数据必须先于使用就绪。"
        )
    if edge.src_port not in outputs:
        raise ExpressionError(
            f"节点 {edge.src!r} 没有输出端口 {edge.src_port!r}（它有：{sorted(outputs) or '无'}）"
        )
    return outputs[edge.src_port]


def resolve_inputs(
    node: Node,
    spec: Mapping[str, Any],
    workflow: Workflow,
    ctx: EvalContext,
) -> dict[str, Any]:
    """按节点声明算出这次调用该传哪些参数。

    只返回"有明确来源"的输入；没填也没连的交给 worker 用默认值，避免内核和插件
    各维护一份默认值、然后慢慢漂移。
    """
    args: dict[str, Any] = {}
    inputs = spec.get("inputs") or {}

    for name in inputs:
        edge = workflow.data_edge_into(node.id, name)
        if edge is not None:
            args[name] = _value_from_edge(edge, ctx)
            continue
        param = node.params.get(name)
        if param is not None and param.mode != "upstream":
            args[name] = resolve_param(param, ctx)

    # 声明里没有、但用户填了的参数也照传，让 worker 去警告"未声明的输入"。
    for name, param in node.params.items():
        if name in args or name in inputs:
            continue
        if param.mode == "upstream":
            edge = workflow.data_edge_into(node.id, name)
            if edge is not None:
                args[name] = _value_from_edge(edge, ctx)
            continue
        args[name] = resolve_param(param, ctx)

    return args
