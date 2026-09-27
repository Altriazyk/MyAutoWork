"""命令行运行器。

    python -m kernel.run --list-plugins
    python -m kernel.run --list-workflows
    python -m kernel.run --validate workflows/demo.json
    python -m kernel.run workflows/demo.json

命令行和图形界面读的是同一份插件接口、同一份工作流 JSON、同一套运行事件。
所以这里能跑通的，界面上也能跑通，反之亦然。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 支持 `python kernel/run.py` 这种直接执行的方式。
if __package__ in (None, ""):  # pragma: no cover - 仅直接执行时进入
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kernel.checklist import (  # noqa: E402
    load as load_checklist_file,
    ordered_workflows,
    resolve_path,
)
from kernel.engine import Engine  # noqa: E402
from kernel.errors import MyAutoWorkError  # noqa: E402
from kernel.graph import load_workflow  # noqa: E402
from kernel.library import WorkflowLibrary  # noqa: E402
from kernel.registry import Registry  # noqa: E402
from kernel.store import RunStore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLUGINS_DIR = REPO_ROOT / "plugins"
DEFAULT_WORKFLOWS_DIR = REPO_ROOT / "workflows"
DEFAULT_HISTORY_PATH = REPO_ROOT / "history.db"

_LEVEL_MARK = {"debug": ".", "info": " ", "warning": "!", "error": "x"}


def _force_utf8() -> None:
    """Windows 控制台默认 cp936，中文日志和符号会炸。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover
            pass


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


# --------------------------------------------------------------------------- #
# 插件目录展示
# --------------------------------------------------------------------------- #


def _field_text(name: str, schema: dict[str, Any]) -> str:
    label = schema.get("label")
    head = f"{label}({name})" if label and label != name else name
    bits = [f"{head}: {schema.get('kind', 'any')}"]
    if schema.get("required"):
        bits.append("必填")
    if "default" in schema:
        bits.append(f"默认={schema['default']!r}")
    if schema.get("choices"):
        bits.append(f"可选={schema['choices']}")
    if schema.get("picker"):
        bits.append(f"带拾取器({schema['picker']})")
    if schema.get("help"):
        bits.append(str(schema["help"]))
    return "  ".join(bits)


def print_catalog(catalog: list[dict[str, Any]], problems: list[tuple[str, str]]) -> None:
    actions = sum(len(p["actions"]) for p in catalog)
    triggers = sum(len(p["triggers"]) for p in catalog)
    print(f"发现 {len(catalog)} 个插件：{triggers} 个触发器，{actions} 个动作")
    print()
    for plugin in catalog:
        loaded = plugin.get("loaded")
        mark = "" if loaded else "   [接口未加载]"
        print(f"* {plugin['id']}   {plugin['name']}   v{plugin['version']}{mark}")
        if plugin.get("description"):
            print(f"    {plugin['description']}")
        if plugin.get("load_error"):
            print(f"    加载失败：{plugin['load_error']}")
        for bucket, label in (("triggers", "触发器"), ("actions", "动作")):
            for spec in plugin[bucket]:
                name = spec.get("name") or spec["id"]
                print(f"    [{label}] {spec['id']}  {name}")
                if spec.get("description"):
                    print(f"          {spec['description']}")
                for port, schema in (spec.get("inputs") or {}).items():
                    print(f"          入  {_field_text(port, schema)}")
                for port, schema in (spec.get("outputs") or {}).items():
                    print(f"          出  {_field_text(port, schema)}")
        print()

    if problems:
        print(f"有 {len(problems)} 个插件没能加载：")
        for where, message in problems:
            print(f"  ! {where}: {message}")


def print_workflows(library: WorkflowLibrary) -> None:
    entries = library.discover()
    print(f"发现 {len(entries)} 条自动化流程（{library.root}）")
    print()
    if not entries:
        print("  （空）")
    for entry in entries:
        status = "解析失败" if not entry.ok else (entry.last_status or "未运行过")
        print(f"* {entry.display_name}")
        if entry.description:
            print(f"    {entry.description}")
        if entry.ok:
            print(f"    {entry.node_count} 个节点 · {entry.edge_count} 条连线 · 上次运行：{status}")
        else:
            print(f"    {entry.error}")
        print(f"    {entry.path}")
        print()
    if library.problems:
        print(f"有 {len(library.problems)} 个文件无法解析。")


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def run_validate(workflow_path: Path, registry: Registry) -> int:
    try:
        workflow = load_workflow(workflow_path)
    except MyAutoWorkError as exc:
        print(f"无法读取工作流：{exc}")
        return 1

    problems = workflow.validate(registry)
    errors = [p for p in problems if p.is_error]
    warnings = [p for p in problems if not p.is_error]

    print(f"校验 {workflow_path}")
    print(f"  {len(workflow.nodes)} 个节点，{len(workflow.edges)} 条边")
    print()
    if not problems:
        print("  没有发现问题。")
        return 0
    for problem in errors:
        print(f"  错误  {problem}")
    for problem in warnings:
        print(f"  警告  {problem}")
    print()
    print(f"结果：{len(errors)} 个错误，{len(warnings)} 个警告")
    return 1 if errors else 0


# --------------------------------------------------------------------------- #
# 运行
# --------------------------------------------------------------------------- #


def run_workflow(
    workflow_path: Path,
    registry: Registry,
    *,
    store: RunStore | None,
    variables: dict[str, Any],
    workdir: Path,
    timeout: float,
) -> int:
    try:
        workflow = load_workflow(workflow_path)
    except MyAutoWorkError as exc:
        print(f"无法读取工作流：{exc}")
        return 1

    problems = workflow.validate(registry)
    errors = [p for p in problems if p.is_error]
    if errors:
        print("工作流校验未通过，已中止：")
        for problem in errors:
            print(f"  {problem}")
        return 1
    for problem in problems:
        print(f"  {problem}")

    def on_event(kind: str, payload: dict[str, Any]) -> None:
        if kind == "node_started":
            print(f"[{_ts()}] >> {payload['node_id']:<4} {payload['type']}")
        elif kind == "node_finished":
            print(f"[{_ts()}] OK {payload['node_id']:<4} {payload['duration_ms'] / 1000:.2f}s")
        elif kind == "node_failed":
            print(f"[{_ts()}] XX {payload['node_id']:<4} 失败：{payload['error']}")
        elif kind == "node_skipped":
            print(f"[{_ts()}] -- {payload['node_id']:<4} 已禁用，跳过")

    def on_log(level: str, message: str, node_id: str) -> None:
        mark = _LEVEL_MARK.get(level, " ")
        print(f"           {mark} [{node_id}] {message}")

    engine = Engine(
        registry,
        store=store,
        default_timeout=timeout,
        workdir=workdir,
        on_event=on_event,
        on_log=on_log,
    )

    workflow_label = f"{workflow.name} ({workflow.id})"
    print(f"运行 {workflow_label}")
    print(f"工作目录 {workdir}")
    print("-" * 64)

    result = engine.run(workflow, variables=variables, workdir=workdir)

    print("-" * 64)
    ok = sum(1 for r in result.node_results if r.status == "success")
    failed = sum(1 for r in result.node_results if r.status == "failed")
    skipped = sum(1 for r in result.node_results if r.status == "skipped")
    verb = {"success": "成功", "failed": "失败", "cancelled": "已停止"}.get(result.status, result.status)
    print(
        f"[{_ts()}] 运行 {result.run_id} {verb}："
        f"{ok} 成功 / {failed} 失败 / {skipped} 跳过，总耗时 {result.duration_ms / 1000:.2f}s"
    )
    if result.error:
        print(f"          原因：{result.error}")
    if store is not None:
        print(f"          历史已写入 {store.path}")
    return 0 if result.ok else 1


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="myautowork",
        description="myautowork 命令行运行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m kernel.run --list-plugins\n"
            "  python -m kernel.run --list-workflows\n"
            "  python -m kernel.run --validate workflows/demo.json\n"
            "  python -m kernel.run workflows/demo.json --var greeting=你好\n"
        ),
    )
    parser.add_argument("workflow", nargs="?", help="要运行的工作流 JSON 文件")
    parser.add_argument("--plugins-dir", type=Path, default=DEFAULT_PLUGINS_DIR, help="插件目录")
    parser.add_argument("--workflows-dir", type=Path, default=DEFAULT_WORKFLOWS_DIR, help="工作流目录")
    parser.add_argument("--list-plugins", action="store_true", help="列出所有插件的接口")
    parser.add_argument("--list-workflows", action="store_true", help="列出所有自动化流程")
    parser.add_argument("--validate", action="store_true", help="只做静态校验，不运行")
    parser.add_argument("--var", action="append", default=[], metavar="KEY=VALUE", help="覆盖工作流变量，可重复")
    parser.add_argument("--workdir", type=Path, default=None, help="运行时工作目录，默认当前目录")
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY_PATH, help="运行历史库路径")
    parser.add_argument("--no-history", action="store_true", help="不写运行历史")
    parser.add_argument("--timeout", type=float, default=300.0, help="单个节点的超时秒数")
    parser.add_argument("--python", default=None, help="启动插件进程用的解释器，默认用当前解释器")
    parser.add_argument(
        "--checklist",
        default="",
        metavar="名字",
        help="按「我的清单」里某条清单的顺序跑完它包含的流程（开机运行走的就是这条）",
    )
    parser.add_argument("--list-checklists", action="store_true", help="列出所有清单")
    parser.add_argument(
        "--checklists-file",
        type=Path,
        default=None,
        help="清单文件路径，默认取工作目录下的 checklists.json",
    )
    return parser


def run_checklist(
    name: str,
    registry: Registry,
    *,
    checklists_path: Path,
    workflows_dir: Path,
    store: RunStore | None,
    variables: dict[str, Any],
    workdir: Path,
    timeout: float,
) -> int:
    """按清单顺序跑完它包含的流程。**这是"开机运行"真正执行的东西。**

    **严格串行，和界面里一致。** 界面自动化会抢鼠标键盘、文件流程会抢同一个文件 —— 两条
    流程同时跑多半是互相踩。开机自动跑的时候更要注意：没人在旁边看着，出问题也没人立刻发现。
    """
    data = load_checklist_file(checklists_path)
    if not data.checklists:
        print(f"清单文件里一条清单都没有：{checklists_path}")
        return 1
    if name not in data.checklists:
        available = "、".join(sorted(data.checklists)) or "（无）"
        print(f"没有叫「{name}」的清单。现有的是：{available}")
        return 1

    names = ordered_workflows(data, name)
    if not names:
        print(f"清单「{name}」是空的，没有可跑的流程")
        return 1

    queue = [workflows_dir / item for item in names]
    missing = [p.name for p in queue if not p.is_file()]
    if missing:
        print(f"清单里有找不到的流程，跳过：{'、'.join(missing)}")
    queue = [p for p in queue if p.is_file()]
    if not queue:
        print("跳完之后一条都不剩，没有可跑的")
        return 1

    print(f"=== 清单「{name}」：{len(queue)} 条流程，按顺序跑（不是并发）===")
    failed: list[str] = []
    for index, path in enumerate(queue, 1):
        print()
        print(f"--- [{index}/{len(queue)}] {path.name} ---")
        code = run_workflow(
            path,
            registry,
            store=store,
            variables=variables,
            workdir=workdir,
            timeout=timeout,
        )
        if code != 0:
            failed.append(path.name)

    print()
    print(f"=== 清单「{name}」结束：{len(queue) - len(failed)}/{len(queue)} 成功 ===")
    if failed:
        print(f"失败的：{'、'.join(failed)}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    # 界面自动化要在本进程做的话（拾取、坐标换算），DPI 感知必须最先开。
    # 插件进程那边由 kernel.worker 自己开，两边保持一致。
    try:
        from myautowork import dpi  # noqa: PLC0415

        dpi.enable()
    except Exception:  # pragma: no cover - 非 Windows 或 SDK 不可用
        pass

    args = build_parser().parse_args(argv)

    variables: dict[str, Any] = {}
    for item in args.var:
        if "=" not in item:
            print(f"--var 需要 KEY=VALUE 形式，收到 {item!r}")
            return 2
        key, _, value = item.partition("=")
        variables[key.strip()] = value

    if not (args.list_plugins or args.list_workflows or args.list_checklists or args.workflow
            or args.checklist):
        build_parser().print_help()
        return 2

    store: RunStore | None = None
    registry = Registry(
        args.plugins_dir,
        repo_root=REPO_ROOT,
        python_exe=args.python,
        on_worker_stderr=lambda line: print(f"           | {line}"),
        worker_timeout=args.timeout,
    )

    try:
        if args.list_workflows:
            if not args.no_history:
                store = RunStore(args.history)
            library = WorkflowLibrary(args.workflows_dir, store=store)
            print_workflows(library)
            return 0

        if args.list_plugins:
            catalog = registry.catalog()
            print_catalog(catalog, registry.problems)
            return 0

        # **清单这两个分支必须在碰 args.workflow 之前。** 用清单跑的时候没有工作流参数，
        # 先走到下面那句 Path(None) 会直接抛 TypeError。
        workdir = (args.workdir or Path.cwd()).resolve()
        checklists_path = args.checklists_file or resolve_path(args.workflows_dir, workdir)

        if args.list_checklists:
            data = load_checklist_file(checklists_path)
            if not data.checklists:
                print(f"还没有清单（{checklists_path}）")
                return 0
            print(f"{checklists_path}")
            for checklist_name in sorted(data.checklists):
                items = data.checklists[checklist_name]
                mark = " [开机运行]" if data.settings_for(checklist_name).startup else ""
                print(f"  {checklist_name}{mark}：{len(items)} 条流程")
            return 0

        if args.checklist:
            if not args.no_history:
                store = RunStore(args.history)
            return run_checklist(
                args.checklist,
                registry,
                checklists_path=checklists_path,
                workflows_dir=args.workflows_dir,
                store=store,
                variables=variables,
                workdir=workdir,
                timeout=args.timeout,
            )

        workflow_path = Path(args.workflow)
        if not workflow_path.is_file():
            print(f"找不到工作流文件：{workflow_path}")
            return 2

        if args.validate:
            return run_validate(workflow_path, registry)

        if not args.no_history:
            store = RunStore(args.history)

        workdir = (args.workdir or Path.cwd()).resolve()
        return run_workflow(
            workflow_path,
            registry,
            store=store,
            variables=variables,
            workdir=workdir,
            timeout=args.timeout,
        )
    except MyAutoWorkError as exc:
        print(f"错误：{exc}")
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断")
        return 130
    finally:
        if store is not None:
            store.close()
        registry.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
