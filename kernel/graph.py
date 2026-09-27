"""工作流数据模型与静态校验。

模型的核心是**两种边**：

- ``exec`` 执行边：决定"先做谁后做谁"。自动化最需要的就是严格时序，所以只有
  exec 边驱动执行。
- ``data`` 数据边：只表示"这个输入从哪取值"，不会让任何节点跑起来。

这个模型同时是磁盘格式（JSON）、内存模型（``Workflow``）和画布的数据源。三者
共用一个定义，就不会出现"界面能连、存盘丢了、引擎不认"这种三头不对马嘴的问题。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import GraphError

__all__ = ["ParamValue", "Node", "Edge", "Workflow", "Problem", "load_workflow", "save_workflow"]

#: 参数取值来源。用"来源"而不是"值"，是为了让用户能在属性面板上把写死的常量
#: 一键换成变量或上游数据，而不用改工作流结构。
MODE_LITERAL = "literal"
MODE_EXPR = "expr"
MODE_UPSTREAM = "upstream"
VALID_MODES = frozenset({MODE_LITERAL, MODE_EXPR, MODE_UPSTREAM})

KIND_EXEC = "exec"
KIND_DATA = "data"

#: exec 边的两个出口。出错分支不是可选项 —— 界面自动化失败率很高，没有错误
#: 分支的工作流只能整条重跑。
EXEC_SUCCESS = "success"
EXEC_ERROR = "error"

#: 内核自己用的两个出口。动作还可以声明**自己的**分支出口（见
#: ``myautowork.decorators.action(branches=...)``），名字由用户填。
RESERVED_EXEC_PORTS = frozenset({EXEC_SUCCESS, EXEC_ERROR})

#: 向后兼容：只含两个固定出口。判断某个端口合不合法请用 ``check_exec_port()``。
VALID_EXEC_PORTS = RESERVED_EXEC_PORTS

#: 分支名最长多少个字。够用了 —— 它的作用是让人一眼看懂这是哪一路。
MAX_EXEC_PORT_CHARS = 40


def branch_names(spec: Mapping[str, Any], params: Mapping[str, Any]) -> list[str]:
    """这个节点有哪些分支出口。没有就返回空列表。

    ``spec["branches"]`` 是动作声明的**参数名**，那个参数的值（一行一条，或者一个列表）
    就是出口名。

    **画布和引擎必须调这同一个函数。** 各写一份的话，两边对"一行里带没带空格""要不要去重"
    的理解迟早会分叉 —— 表现是画布上看得见一个出口、运行时却说没有这条分支，
    而那种 bug 极难查。
    """
    param = str(spec.get("branches") or "").strip()
    if not param:
        return []

    value = params.get(param)
    if value is None:
        return []
    # **两种形状都要认。** 画布上 ``params`` 存的是 ``ParamValue``（带 mode/value），
    # 而流程文件里是裸值。只认裸值的话，界面上改了分支名画布上什么都不会变 ——
    # 表现就是"填了四条分支，出口一个都没多出来"。
    if isinstance(value, ParamValue):
        if value.mode != MODE_LITERAL:
            # 「连线」或「表达式」来的分支名在编辑期算不出来，画布没法画出口。
            # 所以分支名必须是常量 —— 属性面板上要说清楚这一点。
            return []
        value = value.value
    if isinstance(value, str):
        # 属性面板给的是多行文本。逗号也当分隔符 —— 用户很自然会写成一行逗号分隔。
        raw: list[Any] = value.replace("，", ",").replace(",", "\n").splitlines()
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = [value]

    names: list[str] = []
    for item in raw:
        name = str(item).strip()
        # 去重：重名的两个出口在画布上会叠在一起，用户只会觉得"怎么少了一个"。
        if name and name not in names:
            names.append(name)
    return names


def check_exec_port(name: str, *, where: str = "执行端口") -> str:
    """校验一个执行端口名，返回规整后的结果；不合法就抛 ``GraphError``。

    **这里只挡明显非法的，不检查"这个节点到底有没有这个出口"。** 因为读流程文件时
    手上没有插件 schema —— 流程文件要能脱离插件打开，这是刻意的。真正的"有没有这个
    出口"由画布连线时（手上有 schema）和运行时（兜底）把关。
    """
    port = str(name or "").strip()
    if not port:
        raise GraphError(f"{where}不能是空的")
    if len(port) > MAX_EXEC_PORT_CHARS:
        raise GraphError(f"{where}最多 {MAX_EXEC_PORT_CHARS} 个字，收到 {len(port)} 个")
    if any(ch in port for ch in "\r\n\t"):
        raise GraphError(f"{where}不能包含换行或制表符：{port!r}")
    return port


@dataclass
class Problem:
    """一条校验结果。"""

    level: str  # "error" | "warning"
    message: str
    node_id: str | None = None
    edge_index: int | None = None
    code: str = ""

    @property
    def is_error(self) -> bool:
        return self.level == "error"

    def __str__(self) -> str:
        where = f" [节点 {self.node_id}]" if self.node_id else ""
        if self.edge_index is not None:
            where = f" [边 #{self.edge_index}]"
        return f"{self.level.upper()}{where}: {self.message}"


@dataclass
class ParamValue:
    """一个参数值，连同它的来源。"""

    mode: str = MODE_LITERAL
    value: Any = None

    @classmethod
    def parse(cls, raw: Any) -> "ParamValue":
        """接受两种写法：

        - 简写：``10`` / ``"abc"`` → 常量
        - 完整：``{"mode": "expr", "value": "{{ $date }}"}``
        """
        if isinstance(raw, Mapping) and "mode" in raw:
            mode = str(raw["mode"])
            if mode not in VALID_MODES:
                raise GraphError(f"无法识别的参数来源 mode={mode!r}，可选 {sorted(VALID_MODES)}")
            return cls(mode=mode, value=raw.get("value"))
        return cls(mode=MODE_LITERAL, value=raw)

    def to_dict(self) -> Any:
        # 常量用简写存，让工作流文件保持可读、可手改。
        if self.mode == MODE_LITERAL:
            return self.value
        if self.mode == MODE_UPSTREAM:
            return {"mode": MODE_UPSTREAM}
        return {"mode": self.mode, "value": self.value}


@dataclass
class Node:
    """一个节点 = 插件提供的一个能力，加上它的参数配置。"""

    id: str
    plugin: str
    action: str
    pos: tuple[float, float] = (0.0, 0.0)
    params: dict[str, ParamValue] = dc_field(default_factory=dict)
    title: str | None = None
    disabled: bool = False
    timeout: float | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, index: int = 0) -> "Node":
        node_id = str(data.get("id") or "").strip()
        if not node_id:
            raise GraphError(f"第 {index + 1} 个节点缺少 id")
        plugin = str(data.get("plugin") or "").strip()
        action = str(data.get("action") or "").strip()
        if not plugin or not action:
            raise GraphError(
                f"节点 {node_id} 必须同时给出 plugin 和 action"
                "（例如 \"plugin\": \"core.file.write\", \"action\": \"write\"）"
            )

        raw_pos = data.get("pos") or [0, 0]
        try:
            pos = (float(raw_pos[0]), float(raw_pos[1]))
        except (TypeError, ValueError, IndexError) as exc:
            raise GraphError(f"节点 {node_id} 的 pos 必须是 [x, y]") from exc

        params: dict[str, ParamValue] = {}
        raw_params = data.get("params") or {}
        if not isinstance(raw_params, Mapping):
            raise GraphError(f"节点 {node_id} 的 params 必须是对象")
        for name, raw in raw_params.items():
            params[str(name)] = ParamValue.parse(raw)

        timeout = data.get("timeout")
        return cls(
            id=node_id,
            plugin=plugin,
            action=action,
            pos=pos,
            params=params,
            title=data.get("title"),
            disabled=bool(data.get("disabled", False)),
            timeout=float(timeout) if timeout is not None else None,
            notes=str(data.get("notes", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "plugin": self.plugin,
            "action": self.action,
            "pos": [self.pos[0], self.pos[1]],
            "params": {k: v.to_dict() for k, v in self.params.items()},
        }
        if self.title:
            out["title"] = self.title
        if self.disabled:
            out["disabled"] = True
        if self.timeout is not None:
            out["timeout"] = self.timeout
        if self.notes:
            out["notes"] = self.notes
        return out

    @property
    def type_key(self) -> str:
        """画布上唯一标识节点类型，例如 ``core.file.write/write``。"""
        return f"{self.plugin}/{self.action}"

    def param_value(self, name: str, default: Any = None) -> Any:
        pv = self.params.get(name)
        return default if pv is None else pv.value

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"<Node {self.id} {self.type_key}>"


@dataclass
class Edge:
    """一条边。exec 边管顺序，data 边管取值。"""

    src: str
    dst: str
    kind: str = KIND_EXEC
    src_port: str = EXEC_SUCCESS
    dst_port: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, index: int = 0) -> "Edge":
        src = str(data.get("from") or "").strip()
        dst = str(data.get("to") or "").strip()
        if not src or not dst:
            raise GraphError(f"第 {index + 1} 条边必须同时给出 from 和 to")

        kind = str(data.get("kind") or KIND_EXEC)
        if kind not in (KIND_EXEC, KIND_DATA):
            raise GraphError(f"第 {index + 1} 条边的 kind 必须是 'exec' 或 'data'，收到 {kind!r}")

        if kind == KIND_EXEC:
            # 端口名放宽了：动作可以声明自己的分支出口（多路分支）。这里只挡空名、
            # 超长、带换行这些明显非法的 —— 具体"这个节点有没有这个出口"画布和引擎会查。
            src_port = check_exec_port(
                data.get("from_port") or EXEC_SUCCESS, where=f"第 {index + 1} 条执行边的 from_port"
            )
            return cls(src=src, dst=dst, kind=kind, src_port=src_port, dst_port="")

        src_port = str(data.get("from_port") or "").strip()
        dst_port = str(data.get("to_port") or "").strip()
        if not src_port or not dst_port:
            raise GraphError(f"第 {index + 1} 条数据边必须给出 from_port 和 to_port")
        return cls(src=src, dst=dst, kind=kind, src_port=src_port, dst_port=dst_port)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"from": self.src, "to": self.dst, "kind": self.kind}
        if self.kind == KIND_EXEC:
            if self.src_port != EXEC_SUCCESS:
                out["from_port"] = self.src_port
        else:
            out["from_port"] = self.src_port
            out["to_port"] = self.dst_port
        return out

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        if self.kind == KIND_EXEC:
            return f"<Edge exec {self.src}.{self.src_port} -> {self.dst}>"
        return f"<Edge data {self.src}.{self.src_port} -> {self.dst}.{self.dst_port}>"


@dataclass
class Workflow:
    """一整条自动化流程。"""

    id: str = "workflow"
    name: str = "未命名工作流"
    version: int = 1
    description: str = ""
    nodes: dict[str, Node] = dc_field(default_factory=dict)
    edges: list[Edge] = dc_field(default_factory=list)
    variables: dict[str, Any] = dc_field(default_factory=dict)

    # -- 构造 -----------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Workflow":
        if not isinstance(data, Mapping):
            raise GraphError("工作流文件的顶层必须是对象")

        nodes: dict[str, Node] = {}
        for index, raw_node in enumerate(data.get("nodes") or []):
            node = Node.from_dict(raw_node, index=index)
            if node.id in nodes:
                raise GraphError(f"节点 id 重复：{node.id}")
            nodes[node.id] = node

        wf = cls(
            id=str(data.get("id") or "workflow"),
            name=str(data.get("name") or "未命名工作流"),
            version=int(data.get("version", 1)),
            description=str(data.get("description", "")),
            nodes=nodes,
            edges=[],
            variables=dict(data.get("variables") or {}),
        )

        for index, raw_edge in enumerate(data.get("edges") or []):
            edge = Edge.from_dict(raw_edge, index=index)
            # 悬空边在这里就报出来，比运行到一半才发现好得多。
            for endpoint in (edge.src, edge.dst):
                if endpoint not in nodes:
                    raise GraphError(
                        f"第 {index + 1} 条边引用了不存在的节点 {endpoint!r}"
                    )
            wf.edges.append(edge)

        if not wf.nodes:
            raise GraphError("工作流里没有任何节点")
        return wf

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "variables": dict(self.variables),
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
        }

    # -- 查询 -----------------------------------------------------------------

    def node(self, node_id: str) -> Node:
        node = self.nodes.get(node_id)
        if node is None:
            raise GraphError(f"节点不存在：{node_id!r}")
        return node

    def exec_edges_from(self, node_id: str, port: str = EXEC_SUCCESS) -> list[Edge]:
        return [e for e in self.edges if e.kind == KIND_EXEC and e.src == node_id and e.src_port == port]

    def exec_edges_into(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.kind == KIND_EXEC and e.dst == node_id]

    def data_edges_into(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.kind == KIND_DATA and e.dst == node_id]

    def data_edge_into(self, node_id: str, port: str) -> Edge | None:
        for edge in self.edges:
            if edge.kind == KIND_DATA and edge.dst == node_id and edge.dst_port == port:
                return edge
        return None

    def has_exec_in(self, node_id: str) -> bool:
        return any(e.dst == node_id for e in self.edges if e.kind == KIND_EXEC)

    def entry_nodes(self) -> list[Node]:
        """没有执行入边的节点就是起点。"""
        return [n for n in self.nodes.values() if not self.has_exec_in(n.id)]

    def active_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if not n.disabled]

    def plugin_ids(self) -> list[str]:
        return list(dict.fromkeys(n.plugin for n in self.active_nodes()))

    # -- 校验 -----------------------------------------------------------------

    def validate(self, registry: Any | None = None) -> list[Problem]:
        """静态校验。

        有 ``registry`` 时会做端口级检查（端口是否存在、类型是否兼容、必填是否
        接上）；没有时只能做结构检查。界面在编辑过程中会反复调用它。
        """
        problems: list[Problem] = []

        if not self.entry_nodes():
            problems.append(
                Problem("error", "工作流没有起点节点（每个节点都有执行入边，形成了环）", code="no_entry")
            )

        seen_data_inputs: set[tuple[str, str]] = set()
        from myautowork.fields import compatible  # 延迟导入，避免模块级循环依赖

        for index, edge in enumerate(self.edges):
            src = self.nodes.get(edge.src)
            dst = self.nodes.get(edge.dst)
            if src is None or dst is None:
                problems.append(
                    Problem("error", f"边引用了不存在的节点：{edge.src} -> {edge.dst}", edge_index=index)
                )
                continue

            if edge.kind == KIND_DATA:
                if registry is None:
                    continue
                src_spec = registry.node_spec(src.plugin, src.action)
                dst_spec = registry.node_spec(dst.plugin, dst.action)
                if src_spec is None:
                    problems.append(
                        Problem("error", f"无法解析节点类型 {src.type_key}（插件未加载或动作不存在）",
                                node_id=src.id)
                    )
                    continue
                if dst_spec is None:
                    problems.append(
                        Problem("error", f"无法解析节点类型 {dst.type_key}（插件未加载或动作不存在）",
                                node_id=dst.id)
                    )
                    continue

                src_outputs = src_spec.get("outputs") or {}
                dst_inputs = dst_spec.get("inputs") or {}
                if edge.src_port not in src_outputs:
                    problems.append(
                        Problem(
                            "error",
                            f"{src.type_key} 没有名为 {edge.src_port!r} 的输出端口"
                            f"（可用：{sorted(src_outputs) or '无'}）",
                            edge_index=index,
                            code="unknown_output",
                        )
                    )
                    continue
                if edge.dst_port not in dst_inputs:
                    problems.append(
                        Problem(
                            "error",
                            f"{dst.type_key} 没有名为 {edge.dst_port!r} 的输入端口"
                            f"（可用：{sorted(dst_inputs) or '无'}）",
                            edge_index=index,
                            code="unknown_input",
                        )
                    )
                    continue

                src_kind = (src_outputs[edge.src_port] or {}).get("kind", "any")
                dst_kind = (dst_inputs[edge.dst_port] or {}).get("kind", "any")
                if not compatible(src_kind, dst_kind):
                    problems.append(
                        Problem(
                            "warning",
                            f"类型不匹配：{src.type_key}.{edge.src_port} 是 {src_kind}，"
                            f"但 {dst.type_key}.{edge.dst_port} 期望 {dst_kind}。"
                            "运行时会尝试转换，可能失败。",
                            edge_index=index,
                            code="type_mismatch",
                        )
                    )

                # 同一个输入端口接多条线，只有一条会生效 —— 这是用户最容易搞错的地方。
                key = (dst.id, edge.dst_port)
                if key in seen_data_inputs:
                    problems.append(
                        Problem(
                            "warning",
                            f"{dst.id}.{edge.dst_port} 接了多条数据线，运行时只用第一条",
                            edge_index=index,
                            code="duplicate_data_edge",
                        )
                    )
                seen_data_inputs.add(key)

                if dst.param_value(edge.dst_port) is not None:
                    problems.append(
                        Problem(
                            "warning",
                            f"{dst.id}.{edge.dst_port} 同时有连线和参数值，运行时以连线为准",
                            edge_index=index,
                            code="edge_overrides_param",
                        )
                    )

        # 端口级检查：必填输入是否有着落
        if registry is not None:
            for node in self.nodes.values():
                spec = registry.node_spec(node.plugin, node.action)
                if spec is None:
                    problems.append(
                        Problem(
                            "error",
                            f"无法解析节点类型 {node.type_key}：插件 {node.plugin} 未加载或没有动作 {node.action}",
                            node_id=node.id,
                            code="unknown_node_type",
                        )
                    )
                    continue
                inputs = spec.get("inputs") or {}
                connected = {
                    e.dst_port for e in self.edges if e.kind == KIND_DATA and e.dst == node.id
                }

                for name, field_schema in inputs.items():
                    if not (field_schema or {}).get("required"):
                        continue
                    if name in connected:
                        continue
                    if node.param_value(name) is not None:
                        continue
                    problems.append(
                        Problem(
                            "error",
                            f"必填输入 {name!r} 既没有连线也没有填值",
                            node_id=node.id,
                            code="missing_required_input",
                        )
                    )

                extra = set(node.params) - set(inputs)
                for name in sorted(extra):
                    problems.append(
                        Problem(
                            "warning",
                            f"参数 {name!r} 不在该动作的输入声明里，运行时会被忽略",
                            node_id=node.id,
                            code="unknown_param",
                        )
                    )

                for name, pv in node.params.items():
                    if pv.mode == MODE_UPSTREAM and self.data_edge_into(node.id, name) is None:
                        problems.append(
                            Problem(
                                "error",
                                f"参数 {name!r} 的取值来源是「上游连线」，但没有线连到这个输入",
                                node_id=node.id,
                                code="upstream_without_edge",
                            )
                        )

        # 执行环：不一定是错误（循环是合法需求），但值得提示。
        cycle = self._find_exec_cycle()
        if cycle:
            problems.append(
                Problem(
                    "warning",
                    "执行流里存在环：" + " -> ".join(cycle) + "。"
                    "有意为之的话这就是循环（用「流程控制」的「条件判断」构造：判断成立走正文、"
                    "不成立走 error 出口出去，正文末尾连回判断）。执行器有 max_steps 兜底，"
                    "跑飞了会报出来。不是有意的就检查一下连线。",
                    code="exec_cycle",
                )
            )

        return problems

    def _find_exec_cycle(self) -> list[str] | None:
        """用 DFS 找一条执行环，仅用于提示。"""
        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[str, int] = {nid: WHITE for nid in self.nodes}
        stack_path: list[str] = []

        def visit(node_id: str) -> list[str] | None:
            color[node_id] = GREY
            stack_path.append(node_id)
            for edge in self.exec_edges_from(node_id) + self.exec_edges_from(node_id, EXEC_ERROR):
                nxt = edge.dst
                if color.get(nxt) == GREY:
                    start = stack_path.index(nxt)
                    return stack_path[start:] + [nxt]
                if color.get(nxt) == WHITE:
                    found = visit(nxt)
                    if found:
                        return found
            stack_path.pop()
            color[node_id] = BLACK
            return None

        for node_id in list(self.nodes):
            if color[node_id] == WHITE:
                found = visit(node_id)
                if found:
                    return found
        return None

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.validate() if p.is_error]

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"<Workflow {self.id} nodes={len(self.nodes)} edges={len(self.edges)}>"


# --------------------------------------------------------------------------- #
# 读写
# ---------------------------------------------------------------------------


def load_workflow(path: str | Path) -> Workflow:
    file_path = Path(path)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GraphError(f"{file_path} 不是合法 JSON：{exc}") from exc
    except OSError as exc:
        raise GraphError(f"无法读取 {file_path}：{exc}") from exc
    return Workflow.from_dict(data)


def save_workflow(workflow: Workflow, path: str | Path) -> Path:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(workflow.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return file_path
