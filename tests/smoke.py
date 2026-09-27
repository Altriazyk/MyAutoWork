"""内核冒烟测试。不依赖 pytest，直接跑：

    python tests/smoke.py

验证的是最关键的几条契约，而不是实现细节：

1. 插件能被发现、接口能被 describe 出来、manifest 与实际注册一致
2. 工作流校验能通过，四种参数来源（常量 / 表达式 / 上游连线 / 默认值）都能求值
3. 节点输出能被下游用 ``$node.<id>.<port>`` 引用
4. 出错时没有错误分支 → 整条流程失败；有错误分支 → 走错误分支且成功分支不执行
5. 表达式引用不存在的上游输出时，报错信息要能说清原因
6. 工作流库能把磁盘上的流程列出来，并把最近一次运行结果接上
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from kernel.authoring import PluginDraft, PluginDraftError, plugin_template  # noqa: E402
from kernel.engine import Engine, RunResult  # noqa: E402
from kernel.graph import Node, Workflow, load_workflow, save_workflow  # noqa: E402
from kernel.library import WorkflowLibrary  # noqa: E402
from kernel.registry import Registry  # noqa: E402
from kernel.store import RunStore  # noqa: E402
from myautowork import clear_registry  # noqa: E402


class _NullCtx:
    """给"只测纯函数"用的空上下文 —— 能接住日志、存得住变量就行。"""

    def __init__(self) -> None:
        self.variables: dict[str, Any] = {}

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def get_var(self, name: str, default: Any = None) -> Any:
        return self.variables.get(name, default)

    def set_var(self, name: str, value: Any) -> None:
        self.variables[name] = value

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except (AttributeError, ValueError):  # pragma: no cover
    pass


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))

    def section(self, title: str) -> None:
        print(f"\n{title}")


# --------------------------------------------------------------------------- #
# 用内存里的工作流做错误分支实验
# --------------------------------------------------------------------------- #

#: n2 会失败（path 指向一个目录）。它连了错误出口，所以：
#:   - n3 应该被执行（错误分支）
#:   - n4 不应该被执行（成功分支）
#:   - 整次运行的结论仍然算成功
ERROR_HANDLED: dict[str, Any] = {
    "id": "error-handled",
    "name": "错误分支：已接管",
    "nodes": [
        {"id": "n1", "plugin": "core.trigger.manual", "action": "start", "params": {}},
        {
            "id": "n2",
            "plugin": "core.file.write",
            "action": "write",
            # "." 解析成工作目录本身，写文件插件会拒绝并抛错 —— 一个稳定可复现的失败。
            "params": {"path": ".", "content": "这段内容永远写不进去"},
        },
        {
            "id": "n3",
            "plugin": "core.file.write",
            "action": "write",
            "params": {
                "path": "error-report.txt",
                "content": {
                    "mode": "expr",
                    "value": "已接管 {{ $error.type }} 的失败：{{ $error.message }}",
                },
            },
        },
        {
            "id": "n4",
            "plugin": "core.file.write",
            "action": "write",
            "params": {"path": "should-not-exist.txt", "content": "不该被执行"},
        },
    ],
    "edges": [
        {"from": "n1", "to": "n2", "kind": "exec"},
        {"from": "n2", "to": "n3", "kind": "exec", "from_port": "error"},
        {"from": "n2", "to": "n4", "kind": "exec"},
    ],
}

#: 同样的失败节点，但没连错误出口 → 整条流程失败，后续节点一律不跑。
ERROR_UNHANDLED: dict[str, Any] = {
    "id": "error-unhandled",
    "name": "错误分支：未接管",
    "nodes": [
        {"id": "n1", "plugin": "core.trigger.manual", "action": "start", "params": {}},
        {
            "id": "n2",
            "plugin": "core.file.write",
            "action": "write",
            "params": {"path": ".", "content": "注定失败"},
        },
        {
            "id": "n3",
            "plugin": "core.file.write",
            "action": "write",
            "params": {"path": "should-not-exist.txt", "content": "不该被执行"},
        },
    ],
    "edges": [
        {"from": "n1", "to": "n2", "kind": "exec"},
        {"from": "n2", "to": "n3", "kind": "exec"},
    ],
}

#: 表达式引用了不存在的上游输出 → 节点应该失败，且报错要说清"还没执行"。
BAD_EXPRESSION: dict[str, Any] = {
    "id": "bad-expression",
    "name": "表达式引用不存在的上游",
    "nodes": [
        {"id": "n1", "plugin": "core.trigger.manual", "action": "start", "params": {}},
        {
            "id": "n2",
            "plugin": "core.file.write",
            "action": "write",
            "params": {
                "path": "never.txt",
                "content": {"mode": "expr", "value": "{{ $node.n9.text }}"},
            },
        },
    ],
    "edges": [{"from": "n1", "to": "n2", "kind": "exec"}],
}


def executed_ids(result: RunResult) -> list[str]:
    return [r.node_id for r in result.node_results]


def main() -> int:
    checker = Checker()
    plugins_dir = REPO_ROOT / "plugins"
    registry = Registry(plugins_dir, repo_root=REPO_ROOT)
    tmpdir = Path(tempfile.mkdtemp(prefix="myautowork-smoke-"))
    store = RunStore(tmpdir / "history.db")

    try:
        # -- 1. 插件发现与接口 ------------------------------------------------
        checker.section("1) 插件发现与接口描述")
        catalog = registry.catalog()
        checker.check("插件目录里有东西", len(catalog) > 0, str(len(catalog)))
        checker.check("没有加载失败的插件", not registry.problems, str(registry.problems))

        ids = {p["id"] for p in catalog}
        # 内置的几个必须在。不写死总数 —— 加了插件不该让这条测试红。
        builtin = {
            "core.trigger.manual",
            "core.time.delay",
            "core.file.write",
            "core.app",
            "core.flow",
            "win.window",
            "win.input",
            "win.uia",
        }
        checker.check("内置插件 id 齐全", builtin <= ids, str(sorted(builtin - ids)))

        # 界面自动化的三个插件必须声明了 Element 参数，否则拾取器没有落点
        uia_plugin = next((p for p in catalog if p["id"] == "win.uia"), None)
        checker.check("win.uia 已被发现", uia_plugin is not None)
        if uia_plugin:
            element_fields = [
                field
                for action in uia_plugin.get("actions", [])
                for field in (action.get("inputs") or {}).values()
                if field.get("kind") == "element"
            ]
            checker.check(
                "win.uia 的每个动作都有 Element 参数",
                len(element_fields) == len(uia_plugin.get("actions", [])),
                f"{len(element_fields)} 个 element 字段 vs {len(uia_plugin.get('actions', []))} 个动作",
            )
            checker.check(
                "Element 字段自带拾取器",
                all(field.get("picker") == "uia" for field in element_fields),
                str([field.get("picker") for field in element_fields]),
            )

        write_plugin = next((p for p in catalog if p["id"] == "core.file.write"), None)
        write_action = (write_plugin or {}).get("actions", [{}])[0]
        checker.check(
            "写文件插件的 path 声明为 file 类型并带拾取器",
            (write_action.get("inputs", {}).get("path") or {}).get("kind") == "file"
            and (write_action.get("inputs", {}).get("path") or {}).get("picker") == "save",
            str(write_action.get("inputs", {}).get("path")),
        )
        checker.check(
            "content 声明为多行 text",
            (write_action.get("inputs", {}).get("content") or {}).get("kind") == "text",
        )

        # -- 2. 工作流静态校验 ------------------------------------------------
        checker.section("2) 工作流静态校验")
        demo = load_workflow(REPO_ROOT / "workflows" / "demo.json")
        problems = demo.validate(registry)
        errors = [p for p in problems if p.is_error]
        checker.check("demo.json 没有校验错误", not errors, str(errors))
        checker.check("demo.json 没有校验警告", not problems, str(problems))

        # -- 3. 正常运行 ------------------------------------------------------
        checker.section("3) 正常运行（常量 / 表达式 / 上游连线）")
        engine = Engine(registry, store=store, workdir=tmpdir)
        result = engine.run(demo, workdir=tmpdir)
        checker.check("运行成功", result.ok, result.error or "")
        checker.check("4 个节点都成功", all(r.status == "success" for r in result.node_results),
                      str([(r.node_id, r.status) for r in result.node_results]))

        n2 = next((r for r in result.node_results if r.node_id == "n2"), None)
        checker.check("延时节点实际等待了约 1.5 秒",
                      n2 is not None and 1400 <= n2.duration_ms <= 2600,
                      f"{n2.duration_ms if n2 else '?'}ms")

        produced = sorted(tmpdir.glob("out/demo_*.txt"))
        checker.check("文件已生成", len(produced) == 1, str(produced))
        if produced:
            text = produced[0].read_text(encoding="utf-8")
            lines = text.splitlines()
            checker.check("文件有两行（写入 + 追加）", len(lines) == 2, repr(text))
            checker.check(
                "第一行来自上游连线（延时结束时间）",
                bool(lines) and lines[0].startswith("20"),
                repr(lines[0] if lines else ""),
            )
            checker.check(
                "第二行由表达式拼接（含 $node 引用与工作流变量）",
                len(lines) > 1 and "1.5" in lines[1] and "你好，myautowork" in lines[1],
                repr(lines[1] if len(lines) > 1 else ""),
            )

        # -- 4. 错误分支：已接管 ----------------------------------------------
        checker.section("4) 错误分支 —— 已接管")
        handled = Workflow.from_dict(ERROR_HANDLED)
        result = engine.run(handled, workdir=tmpdir)
        checker.check("整次运行算成功（错误已被接管）", result.ok, result.error or "")
        checker.check("执行路径是 n1 -> n2 -> n3", executed_ids(result) == ["n1", "n2", "n3"],
                      str(executed_ids(result)))
        checker.check("失败节点被标记为 failed",
                      any(r.node_id == "n2" and r.status == "failed" for r in result.node_results))
        report = tmpdir / "error-report.txt"
        checker.check("错误分支写出了报告文件", report.is_file(), str(report))
        if report.is_file():
            content = report.read_text(encoding="utf-8")
            checker.check("报告里包含 $error 的内容", "已接管" in content and "IsADirectoryError" in content,
                          repr(content))
        checker.check("成功分支没有被执行", not (tmpdir / "should-not-exist.txt").exists())

        # -- 5. 错误分支：未接管 ----------------------------------------------
        checker.section("5) 错误分支 —— 未接管")
        unhandled = Workflow.from_dict(ERROR_UNHANDLED)
        result = engine.run(unhandled, workdir=tmpdir)
        checker.check("整次运行标记为失败", not result.ok, "却成功了")
        checker.check("错误信息指向出错的节点",
                      bool(result.error) and "n2" in (result.error or ""),
                      result.error or "")
        checker.check("后续节点没有被执行", executed_ids(result) == ["n1", "n2"],
                      str(executed_ids(result)))

        # -- 6. 表达式错误信息 ------------------------------------------------
        checker.section("6) 表达式引用不存在的上游输出")
        bad = Workflow.from_dict(BAD_EXPRESSION)
        result = engine.run(bad, workdir=tmpdir)
        checker.check("运行失败", not result.ok, "却成功了")
        n2_result = next((r for r in result.node_results if r.node_id == "n2"), None)
        message = (n2_result.error if n2_result else "") or ""
        checker.check("报错说明节点还没执行过", "还没有执行" in message, repr(message))

        # -- 7. 破坏 manifest 的插件应该被发现 ---------------------------------
        checker.section("7) 坏插件不应影响其它插件")
        broken_dir = tmpdir / "plugins"
        shutil.copytree(plugins_dir, broken_dir)
        bad_plugin = broken_dir / "core.broken.plugin"
        bad_plugin.mkdir()
        (bad_plugin / "manifest.json").write_text(
            '{"id": "core.broken.plugin", "name": "坏插件", "api_version": "1",'
            ' "provides": {"actions": ["ghost"]}}',
            encoding="utf-8",
        )
        (bad_plugin / "main.py").write_text("# 什么都没注册\n", encoding="utf-8")

        broken_registry = Registry(broken_dir, repo_root=REPO_ROOT)
        broken_catalog = broken_registry.catalog()
        checker.check(
            "好插件仍然可用",
            # **子集判断，不是相等判断。** 相等的话，用户往 plugins/ 里放一个自己的模块
            # 就会让这条测试红 —— 那是正常用法，不是坏消息。库里的东西必须都在，多出来的
            # 是谁的都行。
            builtin | {"core.broken.plugin"} <= {p["id"] for p in broken_catalog},
            str([p["id"] for p in broken_catalog]),
        )
        broken_entry = next((p for p in broken_catalog if p["id"] == "core.broken.plugin"), None)
        checker.check("坏插件被记录为加载失败",
                      bool(broken_entry) and bool(broken_entry.get("load_error")),
                      str(broken_entry))
        checker.check(
            "失败原因清晰可读（而不是只看到进程退出）",
            "没有注册任何 action" in ((broken_entry or {}).get("load_error") or ""),
            repr((broken_entry or {}).get("load_error")),
        )
        broken_registry.shutdown()

        # -- 8. 工作流库 ------------------------------------------------------
        checker.section("8) 工作流库（首页列表的数据源）")
        library_dir = tmpdir / "workflows"
        library_dir.mkdir()
        demo_copy = library_dir / "demo.json"
        shutil.copy2(REPO_ROOT / "workflows" / "demo.json", demo_copy)
        save_workflow(
            Workflow(
                id="second",
                name="第二条流程",
                description="库扫描测试",
                nodes={
                    "n1": Node(id="n1", plugin="core.trigger.manual", action="start"),
                },
            ),
            library_dir / "second.json",
        )
        (library_dir / "broken.json").write_text("{ 这不是 JSON", encoding="utf-8")

        library = WorkflowLibrary(library_dir, store=store)
        entries = library.discover()
        checker.check("扫描到 3 个文件", len(entries) == 3, str([e.path.name for e in entries]))
        by_name = {e.path.name: e for e in entries}

        demo_entry = by_name.get("demo.json")
        checker.check("demo 条目解析成功", demo_entry is not None and demo_entry.ok,
                      str(demo_entry.error if demo_entry else "缺失"))
        checker.check(
            "条目带上了名称/描述/节点数/连线数",
            demo_entry is not None
            and demo_entry.name == demo.name
            and demo_entry.description == demo.description
            and demo_entry.node_count == 4
            and demo_entry.edge_count == 4,
            f"{demo_entry.name if demo_entry else ''} nodes={demo_entry.node_count if demo_entry else '?'}",
        )
        checker.check(
            "最近一次运行被关联上（第 3 节跑过 demo）",
            demo_entry is not None and demo_entry.last_status == "success",
            str(demo_entry.last_run if demo_entry else ""),
        )
        checker.check(
            "坏 JSON 被记录成 error，而不是抛异常",
            by_name.get("broken.json") is not None and not by_name["broken.json"].ok,
            str(by_name.get("broken.json")),
        )
        checker.check("坏文件不影响好文件", library.problems and len(library.problems) == 1,
                      str(library.problems))

        checker.check("按关键字过滤有效",
                      [e.path.name for e in library.filtered("第二条")] == ["second.json"],
                      str([e.path.name for e in library.filtered("第二条")]))
        checker.check("空关键字返回全部", len(library.filtered("")) == 3)

        refreshed = library.refresh_entry(demo_copy)
        checker.check("能单独刷新一条", refreshed is not None and refreshed.ok)

        # -- 9. 插件草稿：AI 创建自定义模块的基础 -----------------------------
        checker.section("9) 插件草稿与落盘")
        import ast as _ast

        code = plugin_template(
            plugin_id="demo.hello", name="打招呼", description="示例插件", category="示例"
        )
        try:
            _ast.parse(code)
            syntax_ok = True
        except SyntaxError:
            syntax_ok = False
        checker.check("模板产出的是合法 Python", syntax_ok)
        checker.check("模板生成的代码能被编译成模块", "from myautowork import" in code)

        good = PluginDraft(
            plugin_id="demo.hello",
            name="打招呼",
            description="示例插件",
            category="示例",
            code=code,
            provides={"actions": ["run"]},
        )
        checker.check("好草稿校验通过", good.ok, str(good.validate()))

        checker.check(
            "非法 id 被拦下",
            any("非法字符" in p for p in PluginDraft(plugin_id="有个空格 的名字", name="x", code=code).validate()),
        )
        checker.check(
            "空代码被拦下",
            any("不能为空" in p for p in PluginDraft(plugin_id="demo.empty", name="x", code="").validate()),
        )
        checker.check(
            "语法错误被拦下",
            any("语法错误" in p for p in PluginDraft(plugin_id="demo.broken", name="x", code="def (:\n").validate()),
        )
        checker.check(
            "空名称被拦下",
            any("名称" in p for p in PluginDraft(plugin_id="demo.noname", name="   ", code=code).validate()),
        )

        draft_dir = tmpdir / "draft_plugins"
        draft_path = good.write(draft_dir)
        checker.check(
            "落盘生成了 manifest.json 与 main.py",
            (draft_path / "manifest.json").is_file() and (draft_path / "main.py").is_file(),
            str(draft_path),
        )
        try:
            good.write(draft_dir)
            blocked = False
        except PluginDraftError:
            blocked = True
        checker.check("重复落盘会被拒绝（不静默覆盖）", blocked)

        # 底线：落盘的插件必须能被内核真正加载起来，而不只是两个文件躺在那
        draft_registry = Registry(draft_dir, repo_root=REPO_ROOT)
        draft_catalog = draft_registry.catalog()
        checker.check(
            "草稿插件能被内核发现并加载",
            len(draft_catalog) == 1 and draft_catalog[0]["id"] == "demo.hello",
            str([p["id"] for p in draft_catalog]),
        )
        checker.check("没有加载错误", not draft_registry.problems, str(draft_registry.problems))
        draft_spec = draft_registry.node_spec("demo.hello", "run")
        checker.check(
            "它的动作接口能被描述出来",
            draft_spec is not None and "text" in (draft_spec.get("inputs") or {}),
            str(draft_spec),
        )

        draft_workflow = Workflow.from_dict(
            {
                "id": "draft-run",
                "name": "跑一下新插件",
                "nodes": [
                    {"id": "n1", "plugin": "demo.hello", "action": "run",
                     "params": {"text": "你好"}},
                ],
                "edges": [],
            }
        )
        draft_engine = Engine(draft_registry, workdir=tmpdir / "draft_work")
        draft_result = draft_engine.run(draft_workflow, workdir=tmpdir / "draft_work")
        checker.check("新插件能被真的跑起来", draft_result.ok, draft_result.error or "")
        checker.check(
            "新插件返回了预期输出",
            draft_result.node_results
            and draft_result.node_results[0].outputs.get("text") == "你好",
            str(draft_result.node_results[0].outputs if draft_result.node_results else None),
        )
        draft_registry.shutdown()

        # -- 10. 依赖声明与检测 -----------------------------------------------
        checker.section("10) 插件依赖：声明与检测")
        from kernel.dependencies import (  # noqa: PLC0415
            check,
            distribution_name,
            from_error,
            missing,
            suggest,
        )

        checker.check("能从 pip 要求里取出包名",
                      distribution_name("openpyxl>=3.1") == "openpyxl"
                      and distribution_name("Pillow") == "Pillow"
                      and distribution_name("requests[security]==2.31") == "requests",
                      distribution_name("requests[security]==2.31"))
        checker.check("取不出包名时不炸，原样返回",
                      distribution_name("") == "" and distribution_name("!!!") == "!!!")

        statuses = check(["Pillow", "完全不存在的包xyz"])
        checker.check("装了的判为已装", statuses[0].installed and bool(statuses[0].version),
                      statuses[0].describe())
        checker.check("没装的判为缺", not statuses[1].installed, statuses[1].describe())
        checker.check("描述里带上了版本", "已装" in statuses[0].describe(), statuses[0].describe())
        checker.check("missing 只返回缺的", missing(["Pillow", "完全不存在的包xyz"]) == ["完全不存在的包xyz"],
                      str(missing(["Pillow", "完全不存在的包xyz"])))
        checker.check("missing 会去重", len(missing(["没有的包a", "没有的包a"])) == 1)
        checker.check("空声明返回空", missing([]) == [] and check([]) == [])

        # manifest 里要能声明依赖，不声明就是空的
        with_deps = PluginDraft(
            plugin_id="demo.withdeps",
            name="要依赖的插件",
            code=code,
            dependencies=["openpyxl>=3.1", "  ", "Pillow"],
        )
        manifest_dict = with_deps.to_manifest()
        checker.check("草稿的依赖写进了 manifest",
                      manifest_dict["dependencies"] == ["openpyxl>=3.1", "  ", "Pillow"],
                      str(manifest_dict.get("dependencies")))

        deps_dir = tmpdir / "dep_plugins"
        with_deps.write(deps_dir)
        from kernel.manifest import load_manifest  # noqa: PLC0415

        loaded = load_manifest(deps_dir / "demo.withdeps")
        checker.check("读回来还是那两条（空白的被丢掉）",
                      loaded.dependencies == ("openpyxl>=3.1", "Pillow"), str(loaded.dependencies))

        plain = PluginDraft(plugin_id="demo.nodeps", name="没依赖的插件", code=code)
        checker.check("不声明就是空的", plain.to_manifest()["dependencies"] == [])

        # 插件目录里声明了依赖的要能被界面查到
        catalog = registry.catalog()
        uia_entry = next((p for p in catalog if p["id"] == "win.uia"), None)
        checker.check("win.uia 声明了依赖",
                      uia_entry is not None and "uiautomation" in (uia_entry.get("dependencies") or []),
                      str((uia_entry or {}).get("dependencies")))
        checker.check("catalog 里带上了缺依赖清单",
                      all("missing_dependencies" in p for p in catalog))
        checker.check("没声明的插件就是空",
                      all(p["dependencies"] == []
                          for p in catalog if p["id"].startswith("core.")),
                      str({p["id"]: p["dependencies"] for p in catalog}))

        # -- 兜底：没声明依赖、但跑起来撞墙的插件 -----------------------------
        checker.check("能从 ModuleNotFoundError 里认出包名",
                      from_error("ModuleNotFoundError: No module named 'openpyxl'") == ["openpyxl"],
                      str(from_error("ModuleNotFoundError: No module named 'openpyxl'")))
        checker.check("只取顶层模块名，并且映射到 pip 上的包名",
                      from_error("No module named 'PIL.Image'") == ["Pillow"],
                      str(from_error("No module named 'PIL.Image'")))
        checker.check("win32 那一堆都映射到 pywin32",
                      from_error("No module named 'win32api'") == ["pywin32"]
                      and from_error("No module named 'win32con'") == ["pywin32"])
        checker.check("同一段报错里的重复只算一次",
                      from_error("No module named 'openpyxl'\nNo module named 'openpyxl'")
                      == ["openpyxl"])
        checker.check("认不出来就是空的（不瞎猜）",
                      from_error("SyntaxError: invalid syntax") == []
                      and from_error("") == [])
        checker.check("suggest 把声明和报错合起来",
                      set(suggest(["Pillow"], "No module named 'openpyxl'")) == {"openpyxl"},
                      str(suggest(["Pillow"], "No module named 'openpyxl'")))
        checker.check("suggest 不会重复报同一个",
                      suggest([], "No module named 'openpyxl'\nNo module named 'openpyxl'")
                      == ["openpyxl"])

        # 端到端：一个**没声明任何依赖**、但 import 不存在的包的插件
        ghost_dir = tmpdir / "ghost_plugins"
        (ghost_dir / "user.ghost").mkdir(parents=True)
        (ghost_dir / "user.ghost" / "manifest.json").write_text(
            json.dumps(
                {
                    "id": "user.ghost",
                    "name": "缺依赖的模块",
                    "api_version": "1",
                    "provides": {"actions": ["run"], "triggers": []},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (ghost_dir / "user.ghost" / "main.py").write_text(
            "import definitely_missing_pkg_xyz\n", encoding="utf-8"
        )
        ghost_registry = Registry(ghost_dir, repo_root=REPO_ROOT)
        ghost_catalog = ghost_registry.catalog()
        ghost = ghost_catalog[0] if ghost_catalog else {}
        checker.check("没声明依赖但撞了墙的插件，界面上也能拿到要装什么",
                      ghost.get("missing_dependencies") == ["definitely_missing_pkg_xyz"],
                      str(ghost.get("missing_dependencies")))
        checker.check("并且记下了失败原因",
                      "definitely_missing_pkg_xyz" in str(ghost.get("load_error") or ""),
                      str(ghost.get("load_error"))[:120])
        ghost_registry.shutdown()

        # -- 11. 找图的坐标换算 -----------------------------------------------
        # 粗找给的是**缩小后**的格子坐标。2 倍缩小时如果直接当原图坐标用，精调会跑到一半
        # 的位置上找，永远找不到 —— 这个 bug 在 find() 里躺了很久，写 find_in() 时才暴露。
        # 这条测试不依赖屏幕，纯图，谁都能跑。
        checker.section("11) 图像匹配：粗找 -> 精调的坐标换算")
        import myautowork.imaging as _imaging  # noqa: PLC0415
        from PIL import Image as _Image  # noqa: PLC0415

        canvas = _Image.new("RGB", (600, 400), (30, 30, 40))
        patch = _Image.new("RGB", (120, 60))
        for _y in range(60):  # 加纹理，免得纯色块在别处也能"完美匹配"
            for _x in range(120):
                patch.putpixel((_x, _y), ((_x * 3) % 200 + 20, (_y * 5) % 200 + 20, 120))
        patch_at = (380, 260)
        canvas.paste(patch, patch_at)
        template_path = tmpdir / "tpl.png"
        patch.save(template_path)

        factor = _imaging._coarse_factor(patch)
        checker.check("这条测试确实落在 2 倍以上的粗找上（否则测不到那个 bug）",
                      factor >= 2, f"factor={factor}")

        found = _imaging.find_in(canvas, template_path, threshold=8.0)
        checker.check(
            "find_in 找到了模板，且坐标是原图的（不是缩小图的）",
            found is not None
            and abs(found[0] - (patch_at[0] + 60)) <= 1
            and abs(found[1] - (patch_at[1] + 30)) <= 1,
            f"{found} 期望中心 ({patch_at[0] + 60}, {patch_at[1] + 30})",
        )
        checker.check("得分是完美匹配", found is not None and found[2] <= 1.0,
                      str(found[2] if found else None))
        checker.check("不存在的模板返回 None，而不是瞎指一个地方",
                      _imaging.find_in(canvas, tmpdir / "没有这张图.png", threshold=8.0) is None)
        checker.check("搜索范围能收窄到正确的那一片",
                      _imaging.find_in(canvas, template_path, threshold=8.0,
                                       region=(300, 200, 560, 380)) is not None)
        checker.check("搜索范围把目标排除掉时返回 None",
                      _imaging.find_in(canvas, template_path, threshold=8.0,
                                       region=(0, 0, 200, 150)) is None)

        # -- 12. core.app：启动程序 --------------------------------------------
        checker.section("12) core.app：启动程序")
        app_entry = next((p for p in registry.catalog() if p["id"] == "core.app"), None)
        checker.check("core.app 是内置插件",
                      app_entry is not None and registry.is_builtin("core.app"))
        checker.check("带 4 个动作",
                      {a["id"] for a in (app_entry or {}).get("actions") or []}
                      == {"launch", "kill", "running", "open_with"},
                      str(sorted(a["id"] for a in (app_entry or {}).get("actions") or [])))

        app_inputs = {
            a["id"]: (a.get("inputs") or {}) for a in (app_entry or {}).get("actions") or []
        }
        checker.check("「启动程序」必须给程序路径 —— 编辑期就该拦住，而不是跑起来才报",
                      app_inputs["launch"]["path"].get("required") is True)
        checker.check("「查进程」必须给进程名",
                      app_inputs["running"]["name"].get("required") is True)
        checker.check("「用默认程序打开」必须填目标",
                      app_inputs["open_with"]["target"].get("required") is True)
        checker.check("启动程序支持等窗口出现，而不是只会睡固定秒数",
                      "window_title" in app_inputs["launch"] and "wait" in app_inputs["launch"])

        # 纯函数：这里藏着两个真实的坑
        import importlib.util  # noqa: PLC0415

        app_spec = importlib.util.spec_from_file_location(
            "core_app_main_for_test", REPO_ROOT / "plugins" / "core.app" / "main.py"
        )
        core_app = importlib.util.module_from_spec(app_spec)
        app_spec.loader.exec_module(core_app)

        checker.check("拆参数：双引号里的空格不算分隔",
                      core_app._split_args('-i "C:\\我的 目录" -v')
                      == ["-i", "C:\\我的 目录", "-v"],
                      str(core_app._split_args('-i "C:\\我的 目录" -v')))
        checker.check("拆参数不会吃掉反斜杠（shlex 的 posix 模式会）",
                      core_app._split_args("C:\\a\\b\\c") == ["C:\\a\\b\\c"],
                      str(core_app._split_args("C:\\a\\b\\c")))
        checker.check("进程名不写 .exe 会自动补上",
                      core_app._normalize_name("MEmu") == "MEmu.exe"
                      and core_app._normalize_name("MEmu.exe") == "MEmu.exe"
                      and core_app._normalize_name("  MEmu  ") == "MEmu.exe",
                      core_app._normalize_name("MEmu"))
        checker.check("查一个绝对不存在的进程：不报错，返回不在跑",
                      core_app.running(_NullCtx(), "绝对没有这个程序xyz")["running"] is False)

        # 上面 exec_module 把动作注册进了 SDK 的全局表，撤掉 —— 别影响别的测试。
        clear_registry()

        # -- 13. core.flow：条件、变量、预期之内的错 ---------------------------
        checker.section("13) core.flow：流程控制")
        from myautowork import ConditionNotMet, ExpectedError  # noqa: PLC0415
        from kernel.worker import _is_expected  # noqa: PLC0415

        checker.check("ConditionNotMet 是 ExpectedError 的子类",
                      issubclass(ConditionNotMet, ExpectedError)
                      and issubclass(ConditionNotMet, RuntimeError))
        checker.check("内核认得这类异常（决定打不打栈）",
                      _is_expected(ConditionNotMet("x")) is True
                      and _is_expected(ExpectedError("x")) is True)
        checker.check("普通异常照旧打完整栈",
                      _is_expected(ValueError("x")) is False
                      and _is_expected(RuntimeError("x")) is False)

        flow_spec = importlib.util.spec_from_file_location(
            "core_flow_main_for_test", REPO_ROOT / "plugins" / "core.flow" / "main.py"
        )
        core_flow = importlib.util.module_from_spec(flow_spec)
        flow_spec.loader.exec_module(core_flow)
        cmp_ = core_flow.compare

        # 数值比较：两边给成字符串也要按数字比（getprop 那类输出天生是字符串）
        checker.check("数字比较：字面数字",
                      cmp_(3, "大于", 2) and cmp_(2, "小于", 3)
                      and cmp_(3, "大于等于", 3) and cmp_(3, "小于等于", 3))
        checker.check("数字比较：字符串形式的数字也认",
                      cmp_("10", "大于", "9"), "字符串比会得出 '10' < '9'")
        checker.check("数字比较：整数和小数混着来",
                      cmp_(3, "等于", 3.0) and cmp_("2.5", "大于", 2))

        # 相等用松散比较："3" 和 3 应该算相等
        checker.check("相等：字符串和数字算相等",
                      cmp_("3", "等于", 3) and cmp_(3, "等于", "3"))
        checker.check("相等：前后空白不算差别", cmp_("  3 ", "等于", "3"))
        checker.check("不等于就是取反", cmp_("3", "不等于", 4) and not cmp_(3, "不等于", 3))

        # **不能静默判错**：数字比较碰上非数字必须报错，而不是拿 0 兜底
        try:
            cmp_("abc", "大于", 3)
        except ConditionNotMet as exc:
            checker.check("数字比较碰上非数字会明确报错（不是静默判成假）",
                          "转不过去" in str(exc), str(exc))
        else:
            checker.check("数字比较碰上非数字会明确报错（不是静默判成假）", False,
                          "居然没抛异常 —— 拿 0 兜底会让 3 > 0 静默成立")

        checker.check("包含：列表 / 字符串 / 字典",
                      cmp_(["a", "b"], "包含", "a")
                      and cmp_("hello world", "包含", "world")
                      and cmp_({"k": 1}, "包含", "k"))
        checker.check("不包含是取反", cmp_("hello", "不包含", "zzz"))
        checker.check("为空 / 不为空，各种空法都认",
                      cmp_("", "为空", None) and cmp_("   ", "为空", None)
                      and cmp_(None, "为空", None) and cmp_([], "为空", None)
                      and cmp_({}, "为空", None) and cmp_(0, "不为空", None))
        checker.check("为真 / 为假：字符串的 false 要当成假",
                      cmp_("true", "为真", None) and cmp_(1, "为真", None)
                      and not cmp_("false", "为真", None)
                      and cmp_("false", "为假", None) and cmp_("", "为假", None))
        try:
            cmp_(1, "不认识的运算符", 2)
        except ConditionNotMet:
            checker.check("不认识的运算符会报错", True)
        else:
            checker.check("不认识的运算符会报错", False)

        # 动作本身
        ctx = _NullCtx()
        checker.check("条件成立时正常返回，不抛异常",
                      core_flow.if_(ctx, left=5, op="大于", right=3)["op"] == "大于")
        try:
            core_flow.if_(ctx, left=1, op="大于", right=3)
        except ConditionNotMet:
            checker.check("条件不成立时抛 ConditionNotMet（走 error 出口）", True)
        else:
            checker.check("条件不成立时抛 ConditionNotMet（走 error 出口）", False)

        checker.check("设变量：第一次相加按 0 起算，不会 None+1 炸掉",
                      core_flow.set_var(ctx, "n", 1, "相加")["value"] == 1)
        checker.check("设变量：相加能累加", core_flow.set_var(ctx, "n", 2, "相加")["value"] == 3)
        checker.check("设变量：整数运算不显示成 3.0",
                      isinstance(core_flow.set_var(ctx, "n", 1, "相加")["value"], int))
        checker.check("设变量：追加是接在原文后面",
                      core_flow.set_var(ctx, "s", "a", "设置")["value"] == "a"
                      and core_flow.set_var(ctx, "s", "b", "追加")["value"] == "ab")
        try:
            core_flow.set_var(ctx, "n", "不是数字", "相加")
        except ValueError:
            checker.check("相加碰上非数字会报错", True)
        else:
            checker.check("相加碰上非数字会报错", False)

        checker.check("等待文件：已经存在就立刻返回，不白等",
                      core_flow.wait_file(ctx, str(REPO_ROOT / "README.md"), timeout=5)["waited"] < 1.0)
        try:
            core_flow.wait_file(ctx, str(tmpdir / "永远不会有这个文件"), timeout=0.3, interval=0.1)
        except ConditionNotMet:
            checker.check("等不到文件时抛 ConditionNotMet（不是死等）", True)
        else:
            checker.check("等不到文件时抛 ConditionNotMet（不是死等）", False)

        checker.check("core.flow 是内置插件，动作都在",
                      registry.is_builtin("core.flow")
                      and {a["id"] for a in next(
                          p for p in registry.catalog() if p["id"] == "core.flow")["actions"]}
                      == {"if", "repeat", "for_each", "set_var", "math", "string", "list",
                          "dict", "assert", "fail", "log", "end", "random_wait",
                          "wait_file", "wait_process"})
        clear_registry()

        # **循环真的能跑**：用「条件判断」+ 回边构造一个三轮循环，交给执行器跑。
        # 这条测试是整个控制流的地基 —— 它同时验证了：环会被执行器迭代、false 分支
        # 走 error 出口、以及"条件为假"不会把整条流程标成失败。
        loop_workflow = Workflow.from_dict(
            {
                "nodes": [
                    {"id": "n1", "plugin": "core.flow", "action": "set_var",
                     "params": {"name": "n", "value": 0, "mode": "设置"}},
                    {"id": "n2", "plugin": "core.flow", "action": "if",
                     "params": {"left": {"mode": "expr", "value": "{{ $vars.n }}"},
                                "op": "小于", "right": 3}},
                    {"id": "n3", "plugin": "core.flow", "action": "log",
                     "params": {"message": {"mode": "expr",
                                            "value": "循环第 {{ $vars.n }} 次"}}},
                    {"id": "n4", "plugin": "core.flow", "action": "set_var",
                     "params": {"name": "n", "value": 1, "mode": "相加"}},
                    {"id": "n5", "plugin": "core.flow", "action": "end",
                     "params": {"note": "循环跑完了"}},
                ],
                "edges": [
                    {"kind": "exec", "from": "n1", "from_port": "success", "to": "n2", "to_port": "in"},
                    {"kind": "exec", "from": "n2", "from_port": "success", "to": "n3", "to_port": "in"},
                    {"kind": "exec", "from": "n2", "from_port": "error", "to": "n5", "to_port": "in"},
                    {"kind": "exec", "from": "n3", "from_port": "success", "to": "n4", "to_port": "in"},
                    {"kind": "exec", "from": "n4", "from_port": "success", "to": "n2", "to_port": "in"},
                ],
            }
        )
        loop_problems = loop_workflow.validate(registry)
        checker.check("环只报警告，不判错误（循环是合法需求）",
                      not [p for p in loop_problems if p.is_error],
                      str([str(p) for p in loop_problems]))

        loop_dir = tmpdir / "loop_work"
        loop_result = Engine(registry, workdir=loop_dir).run(loop_workflow, workdir=loop_dir)
        loop_nodes = loop_result.to_dict()["nodes"]
        checker.check("循环被真正迭代了（n2 执行了 4 次：3 次成立 + 1 次不成立）",
                      sum(1 for x in loop_nodes if x["node_id"] == "n2") == 4,
                      str(sum(1 for x in loop_nodes if x["node_id"] == "n2")))
        checker.check("正文跑了 3 遍",
                      sum(1 for x in loop_nodes if x["node_id"] == "n3") == 3)
        checker.check("循环出口走到了（n5 执行了）",
                      any(x["node_id"] == "n5" for x in loop_nodes))
        checker.check("**条件为假不会把整条流程标成失败**",
                      loop_result.status == "success", loop_result.status)
        checker.check("循环结束后变量是终值 3", loop_result.to_dict()["variables"]["n"] == 3,
                      str(loop_result.to_dict()["variables"]))
        false_node = next(x for x in loop_nodes if x["node_id"] == "n2" and x["status"] == "failed")
        checker.check("false 分支的报错是一句人话，不是栈",
                      "条件不成立" in (false_node.get("error") or "")
                      and "Traceback" not in (false_node.get("error") or ""),
                      str(false_node.get("error")))

    finally:
        store.close()
        registry.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n{'=' * 56}")
    print(f"通过 {checker.passed} 项，失败 {checker.failed} 项")
    return 1 if checker.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
