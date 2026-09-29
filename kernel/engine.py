"""执行流 + 数据流双线执行引擎。

五条规则，整个引擎就是它们的实现：

1. **只有 exec 边驱动执行。** data 边只表示取值来源，不会让节点跑起来。
2. **执行节点前先解析全部输入**（常量 / 表达式 / 上游连线）。
3. **节点输出缓存进 ``$node.<id>.<port>``**，任何后续节点都能用表达式取到。
4. **允许 data 边连到执行顺序更早的节点**，运行时取不到值就明确报错 —— 静态判定
   不了（有分支和循环），所以选择"能连 + 运行时报错"。
5. **一个 exec 出口连多条线是顺序执行，不是并行。** 界面自动化里隐式并行会造成
   几乎无法复现的 bug；要并行必须显式用并行节点。

还有两个配套约定：

- 每个节点有 ``success`` / ``error`` 两个 exec 出口。``error`` 没连线就向上冒泡、
  整条流程标记失败；连了就走错误分支。
- 出错时把 ``$error``（含 node_id / message / type）放进变量，错误分支可以直接引用。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import MyAutoWorkError, RunCancelled, WorkflowError
from .expr import EvalContext, make_builtins, resolve_inputs
from .graph import EXEC_ERROR, EXEC_SUCCESS, Node, Workflow, branch_names
from .rpc import FOREVER

__all__ = ["NodeResult", "RunResult", "Engine", "new_run_id"]

EventHook = Callable[[str, dict[str, Any]], None]
LogHook = Callable[[str, str, str], None]


def new_run_id() -> str:
    return f"run_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"




def _as_branch_list(value: Any) -> list[str]:
    """把分支参数的值规整成 ``["分支甲", "分支乙"]``。

    实现搬到了 :func:`kernel.graph.branch_names` —— **画布也要用同一份规则**，
    各写一份迟早分叉。这里保留这个名字只是为了不打断调用点。
    """
    return branch_names({"branches": "value"}, {"value": value})


@dataclass
class NodeResult:
    node_id: str
    plugin: str
    action: str
    title: str | None
    status: str  # success | failed | skipped
    started_at: str
    duration_ms: int
    outputs: dict[str, Any] = dc_field(default_factory=dict)
    error: str | None = None
    logs: list[dict[str, Any]] = dc_field(default_factory=list)

    @property
    def type_key(self) -> str:
        return f"{self.plugin}/{self.action}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "plugin": self.plugin,
            "action": self.action,
            "title": self.title,
            "type": self.type_key,
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "outputs": self.outputs,
            "error": self.error,
            "logs": self.logs,
        }


@dataclass
class RunResult:
    run_id: str
    workflow_id: str
    workflow_name: str
    status: str  # success | failed | cancelled
    started_at: str
    finished_at: str
    duration_ms: int
    node_results: list[NodeResult] = dc_field(default_factory=list)
    error: str | None = None
    variables: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def cancelled(self) -> bool:
        return self.status == "cancelled"

    @property
    def failed_node(self) -> NodeResult | None:
        for result in self.node_results:
            if result.status == "failed":
                return result
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "variables": self.variables,
            "nodes": [r.to_dict() for r in self.node_results],
        }


class _RunState:
    """一次运行的可变状态。通知回调靠它把日志路由到正确的节点上。"""

    def __init__(self, engine: "Engine", ctx: EvalContext) -> None:
        self.engine = engine
        self.ctx = ctx
        self.results: list[NodeResult] = []
        self.logs: dict[str, list[dict[str, Any]]] = {}
        self.current_node_id: str = ""
        self.seq: int = 0


class Engine:
    """把工作流跑起来。"""

    def __init__(
        self,
        registry: Any,
        *,
        store: Any | None = None,
        default_timeout: float = 300.0,
        max_steps: int = 10000,
        workdir: str | Path | None = None,
        on_event: EventHook | None = None,
        on_log: LogHook | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.default_timeout = default_timeout
        # 死循环保护。循环节点是合法需求，所以不能靠"禁止重复执行"来防，
        # 只能给总步数设上限。
        self.max_steps = max_steps
        self.workdir = Path(workdir).resolve() if workdir else Path.cwd()
        self.on_event = on_event
        self.on_log = on_log
        self._state: _RunState | None = None
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """请求停止：当前节点会跑完，之后立即停止，并继续执行收尾（写历史、发事件）。

        **没有"强制停止"这个选项，是试过之后撤掉的。** 做法本该是：把正在跑的那个插件
        进程杀掉，让引擎那次阻塞的等待以异常结束。

        实测不行。引擎阻塞在 ``client.request(...)`` 上等插件回话，而杀掉进程**不一定让
        那次等待醒过来** —— 插件起过的孙进程（adb、ping 之类）继承了输出管道的写端，
        进程死了管道还开着，读端就一直等到那次请求自己超时（默认 300 秒）。表现是
        "点了停止没反应"，比不强杀还糟。

        真要做得先解决两件事：杀进程之后**主动关掉管道**让读端立刻收到 EOF；以及让读取
        循环可中断，而不是依赖"对方一死管道就断"这个在子孙进程场景下不成立的前提。
        """
        self._cancel.set()

    # -- 主流程 ---------------------------------------------------------------

    def run(
        self,
        workflow: Workflow,
        *,
        variables: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        workdir: str | Path | None = None,
    ) -> RunResult:
        run_id = run_id or new_run_id()
        run_workdir = Path(workdir).resolve() if workdir else self.workdir
        started = datetime.now()
        self._cancel.clear()

        ctx = EvalContext(
            run_id=run_id,
            workflow=workflow,
            workdir=run_workdir,
            variables=dict(workflow.variables),
            cache={},
            builtins=make_builtins(run_id=run_id, workflow=workflow, workdir=run_workdir),
        )
        if variables:
            ctx.variables.update(variables)

        state = _RunState(self, ctx)
        self._state = state
        # 插件推回的日志/进度经由 registry 转发到这里。引擎是唯一的运行方，
        # 所以单一活动状态是够的；将来要并行跑多条工作流时，这里换成按 run_id 路由。
        self.registry.add_listener(self._on_notification)

        if self.store is not None:
            self.store.start_run(
                run_id=run_id,
                workflow_id=workflow.id,
                workflow_name=workflow.name,
                started_at=started.isoformat(timespec="seconds"),
            )

        self._emit(
            "run_started",
            {"run_id": run_id, "workflow_id": workflow.id, "workflow_name": workflow.name},
        )

        status = "success"
        error: str | None = None
        try:
            self._walk(workflow, ctx, state)
        except RunCancelled:
            status, error = "cancelled", "运行已取消"
        except MyAutoWorkError as exc:
            status, error = "failed", str(exc)
        except Exception as exc:  # pragma: no cover - 兜底，避免把异常泄漏给界面
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self.registry.remove_listener(self._on_notification)
            self._state = None

        finished = datetime.now()
        duration_ms = int((finished - started).total_seconds() * 1000)

        for result in state.results:
            result.logs = state.logs.get(result.node_id, [])

        run_result = RunResult(
            run_id=run_id,
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            status=status,
            started_at=started.isoformat(timespec="seconds"),
            finished_at=finished.isoformat(timespec="seconds"),
            duration_ms=duration_ms,
            node_results=state.results,
            error=error,
            variables=dict(ctx.variables),
        )

        if self.store is not None:
            self.store.finish_run(
                run_id=run_id,
                status=status,
                finished_at=run_result.finished_at,
                duration_ms=duration_ms,
                error=error,
            )

        self._emit("run_finished", run_result.to_dict())
        return run_result

    # -- 遍历 -----------------------------------------------------------------

    def _walk(self, workflow: Workflow, ctx: EvalContext, state: _RunState) -> None:
        # **纯数据节点先跑一遍。**
        #
        # 它们是"值的来源"（画布上那个「值」节点）：不参与执行顺序，只靠数据线喂给
        # 下游。不先算出来的话，下游 `upstream` 取到的就是空的 —— 而"有没有先算"
        # 取决于**节点在文件里的先后**，也就是你先拖的哪个。
        #
        # 判据是**动作的声明**（``data_only=True``），不是"它有没有执行连线" ——
        # 后者会把一个刚拖进来、还没连线的普通节点也误判成数据节点，
        # 那样它的执行口会在画布上莫名其妙地消失。
        pure = [
            node
            for node in workflow.nodes.values()
            if self._is_data_only(node)
        ]
        pure_ids = {node.id for node in pure}
        for node in pure:
            self._run_node(node, workflow, ctx, state)

        entries = [
            node
            for node in workflow.nodes.values()
            if not workflow.has_exec_in(node.id) and node.id not in pure_ids
        ]
        if not entries:
            raise WorkflowError("工作流没有起点节点（每个节点都有执行入边，形成了环）")

        # 用栈做深度优先，保证分叉时"第一条分支走完再走第二条"，与画布上的直觉一致。
        stack: list[Node] = list(reversed(entries))
        steps = 0

        while stack:
            if self._cancel.is_set():
                raise RunCancelled("运行已取消")
            node = stack.pop()
            steps += 1
            if steps > self.max_steps:
                raise WorkflowError(
                    f"执行步数超过上限 {self.max_steps}，可能存在死循环。"
                    "若这是有意为之的循环，请调高 max_steps。"
                )
            port = self._run_node(node, workflow, ctx, state)
            successors = workflow.exec_edges_from(node.id, port)
            for edge in reversed(successors):
                target = workflow.nodes.get(edge.dst)
                if target is None:  # 已被 from_dict 拦下，这里只是防御
                    continue
                stack.append(target)

    # -- 单节点 ---------------------------------------------------------------

    def _run_node(
        self,
        node: Node,
        workflow: Workflow,
        ctx: EvalContext,
        state: _RunState,
    ) -> str:
        started_at = datetime.now().isoformat(timespec="seconds")

        if node.disabled:
            state.results.append(
                NodeResult(
                    node_id=node.id,
                    plugin=node.plugin,
                    action=node.action,
                    title=node.title,
                    status="skipped",
                    started_at=started_at,
                    duration_ms=0,
                )
            )
            state.logs.setdefault(node.id, []).append(
                {"node_id": node.id, "level": "debug", "message": "节点已禁用，跳过"}
            )
            self._emit("node_skipped", {"node_id": node.id})
            return EXEC_SUCCESS

        spec = self.registry.node_spec(node.plugin, node.action)
        if spec is None:
            raise WorkflowError(
                f"节点 {node.id} 的类型 {node.type_key} 无法解析："
                f"插件 {node.plugin} 未能加载，或它没有动作 {node.action}"
            )

        state.current_node_id = node.id
        state.logs.setdefault(node.id, [])
        self._emit(
            "node_started",
            {
                "node_id": node.id,
                "type": node.type_key,
                "title": node.title,
                "plugin": node.plugin,
                "action": node.action,
            },
        )

        clock = time.perf_counter()
        try:
            args = resolve_inputs(node, spec, workflow, ctx)
            self._check_required(node, spec, args)

            client = self.registry.client(node.plugin)
            timeout = node.timeout if node.timeout is not None else self.default_timeout
            # **0 表示不限时。** 有些动作本来就要等很久（等一个任务跑完、等视频播完、
            # 等人操作），给它们一个上限只会让长任务中途被杀掉 —— 而那个结果
            # （"插件在 N 秒内没有响应，进程已终止"）看起来像插件崩了，不像超时。
            #
            # 传的是 ``rpc.FOREVER``，**不是一个大数**。曾经用"足够大的秒数"糊过，
            # 结果 31 亿秒把底层锁的毫秒参数撑爆，报 `OverflowError: timeout value
            # is too large` —— 大数不等于无限，底层到处都有位宽限制。
            request_timeout = timeout if timeout else FOREVER
            response = client.request(
                "invoke",
                {
                    "action_id": node.action,
                    "node_id": node.id,
                    "run_id": ctx.run_id,
                    "params": args,
                    "variables": ctx.variables,
                    "workdir": str(ctx.workdir),
                    "artifacts_dir": str(ctx.workdir / "artifacts"),
                },
                timeout=request_timeout,
            )

            outputs = dict(response.get("output") or {})
            ctx.cache[node.id] = outputs

            returned_vars = response.get("variables")
            if isinstance(returned_vars, dict):
                ctx.variables.update(returned_vars)

            duration_ms = int(response.get("duration_ms") or (time.perf_counter() - clock) * 1000)
            result = NodeResult(
                node_id=node.id,
                plugin=node.plugin,
                action=node.action,
                title=node.title,
                status="success",
                started_at=started_at,
                duration_ms=duration_ms,
                outputs=outputs,
            )
            state.results.append(result)
            self._record(result)
            self._emit(
                "node_finished",
                {"node_id": node.id, "duration_ms": duration_ms, "outputs": outputs},
            )
            return self._exec_port_for(node, spec, outputs, args)

        except Exception as exc:
            duration_ms = int((time.perf_counter() - clock) * 1000)
            message = str(exc) if isinstance(exc, MyAutoWorkError) else f"{type(exc).__name__}: {exc}"
            result = NodeResult(
                node_id=node.id,
                plugin=node.plugin,
                action=node.action,
                title=node.title,
                status="failed",
                started_at=started_at,
                duration_ms=duration_ms,
                error=message,
            )
            state.results.append(result)
            self._record(result)
            self._emit("node_failed", {"node_id": node.id, "error": message, "duration_ms": duration_ms})

            # 连了错误出口就走错误分支，没连就整条流程失败。
            if workflow.exec_edges_from(node.id, EXEC_ERROR):
                ctx.variables["error"] = {
                    "node_id": node.id,
                    "type": node.type_key,
                    "message": message,
                }
                return EXEC_ERROR
            raise WorkflowError(f"节点 {node.id}（{node.type_key}）执行失败：{message}") from exc

    def _is_data_only(self, node: Node) -> bool:
        """这个节点是不是"只出数据"的（画布上那个「值」）。

        看**动作的声明**（``data_only=True``）。拿不到声明就当普通节点 ——
        宁可多跑一个节点，也不要把一个普通节点错当成数据节点（那样它的执行口会在
        画布上消失，而用户不知道为什么）。
        """
        try:
            spec = self.registry.node_spec(node.plugin, node.action)
        except Exception:
            return False
        return bool(spec.get("data_only"))

    def _exec_port_for(
        self,
        node: Node,
        spec: Mapping[str, Any],
        outputs: dict[str, Any],
        args: Mapping[str, Any],
    ) -> str:
        """这个节点跑完之后走哪个执行出口。

        动作没声明 ``branches`` 就还是老规矩：成功走 ``success``。

        声明了就要求它返回里带一个 ``"branch"`` 键，值是某一路的名字。**这里要严格校验** ——
        返回一个不存在的分支名，如果放过去，执行流会走到一条不存在的边上，表现是
        "流程跑着跑着就没了"，那是很难查的。宁可在这一步报清楚。
        """
        param = str(spec.get("branches") or "").strip()
        if not param:
            return EXEC_SUCCESS

        chosen = outputs.pop("branch", None)
        # 和画布共用同一份解析规则（kernel.graph.branch_names）。
        declared = branch_names(spec, args)

        if not declared:
            raise WorkflowError(
                f"节点 {node.id}（{node.type_key}）的分支参数「{param}」是空的 —— "
                "它没有任何出口可走，去属性面板里至少加一条分支"
            )
        if chosen is None:
            raise WorkflowError(
                f"节点 {node.id}（{node.type_key}）声明了分支出口，但结果里没有 branch。"
                f"它应该返回一个 branch，值是这些之一：{'、'.join(declared)}"
            )
        chosen = str(chosen).strip()
        if chosen not in declared:
            raise WorkflowError(
                f"节点 {node.id}（{node.type_key}）要走的出口是「{chosen}」，"
                f"但它声明的分支里没有这一条。可用的分支：{'、'.join(declared)}"
            )
        return chosen

    def _check_required(self, node: Node, spec: Mapping[str, Any], args: Mapping[str, Any]) -> None:
        missing = [
            name
            for name, schema in (spec.get("inputs") or {}).items()
            if (schema or {}).get("required") and name not in args
        ]
        if missing:
            raise WorkflowError(
                f"必填输入没有值：{', '.join(missing)}（既没有连线，也没有填参数）"
            )

    def _record(self, result: NodeResult) -> None:
        # 逐条落库：长流程跑到一半崩掉时，已完成节点的记录仍然在。
        state = self._state
        if self.store is None or state is None:
            return
        self.store.record_nodes(state.ctx.run_id, [result], start_seq=state.seq)
        state.seq += 1

    # -- 通知与事件 -----------------------------------------------------------

    def _on_notification(self, payload: dict[str, Any]) -> None:
        state = self._state
        if state is None:
            return

        level = str(payload.get("level") or "info")
        message = str(payload.get("message") or "")

        # 插件里 ctx.set_var() 走的是通知通道，界面能实时看到变量变化。
        if level == "set_var":
            state.ctx.variables[message] = payload.get("value")
            self._emit(
                "variable_changed",
                {"node_id": payload.get("node_id", ""), "name": message, "value": payload.get("value")},
            )
            return

        if level == "progress":
            self._emit(
                "node_progress",
                {
                    "node_id": payload.get("node_id") or state.current_node_id,
                    "progress": payload.get("progress"),
                    "message": message,
                },
            )
            return

        node_id = str(payload.get("node_id") or state.current_node_id)
        entry = {"node_id": node_id, "level": level, "message": message}
        state.logs.setdefault(node_id, []).append(entry)
        self._emit("log", entry)
        if self.on_log is not None:
            self.on_log(level, message, node_id)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, payload)
            except Exception:  # pragma: no cover - 订阅者出错不该中断执行
                pass
