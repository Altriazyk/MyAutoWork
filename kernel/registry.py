"""插件发现与生命周期管理。

对界面而言，这个类是**唯一**接触插件的入口。界面拿到的是纯 JSON 的接口描述，
永远不需要知道背后是一个子进程、还是以后会换成别的执行载体。

启动策略是**按需**的：发现插件只读 manifest（毫秒级，几十个插件也无所谓），
真正拉起进程推迟到第一次要用它的接口或执行它的动作。这样应用启动速度不会被
"装了一百个插件"拖垮。
"""

from __future__ import annotations

import atexit
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .dependencies import suggest
from .errors import ManifestError, PluginError
from .manifest import MANIFEST_FILENAME, Manifest, load_manifest
from .recycle import to_recycle_bin
from .rpc import WorkerClient

__all__ = ["Registry", "PluginRecord"]

NotificationHandler = Callable[[dict[str, Any]], None]


class PluginRecord:
    """一个插件在内核里的全部已知信息。"""

    def __init__(self, manifest: Manifest) -> None:
        self.manifest = manifest
        self.client: WorkerClient | None = None
        self.interface: dict[str, Any] | None = None
        self.load_error: str | None = None

    @property
    def id(self) -> str:
        return self.manifest.id

    def node_spec(self, action_id: str) -> dict[str, Any] | None:
        """查一个动作/触发器的接口描述。"""
        if not self.interface:
            return None
        for bucket in ("actions", "triggers"):
            spec = (self.interface.get(bucket) or {}).get(action_id)
            if spec is not None:
                return spec
        return None

    def all_nodes(self) -> Iterator[dict[str, Any]]:
        if not self.interface:
            return
        for bucket in ("triggers", "actions"):
            for spec in (self.interface.get(bucket) or {}).values():
                yield spec


class Registry:
    """插件目录的索引 + worker 进程池。"""

    def __init__(
        self,
        plugins_dir: str | Path,
        *,
        repo_root: str | Path | None = None,
        python_exe: str | None = None,
        on_notification: NotificationHandler | None = None,
        on_worker_stderr: Callable[[str], None] | None = None,
        worker_timeout: float = 300.0,
    ) -> None:
        self.plugins_dir = Path(plugins_dir).resolve()
        self.repo_root = Path(repo_root).resolve() if repo_root else self.plugins_dir.parent
        self.python_exe = python_exe
        # 通知用订阅制：执行引擎、界面日志面板可以各订各的，互不干扰。
        self._listeners: list[NotificationHandler] = []
        if on_notification is not None:
            self._listeners.append(on_notification)
        self.on_worker_stderr = on_worker_stderr
        self.worker_timeout = worker_timeout

        self._records: dict[str, PluginRecord] = {}
        #: 加载失败的插件，形如 [(目录, 原因)]。不抛异常，让界面能报告"哪个插件坏了"。
        self.problems: list[tuple[str, str]] = []
        self._discovered = False
        self._closed = False
        atexit.register(self.shutdown)

    # -- 通知订阅 -------------------------------------------------------------

    def add_listener(self, handler: NotificationHandler) -> None:
        """订阅插件推回的通知（日志、进度、变量写入）。

        执行引擎靠它在运行期把日志和进度路由到正确的节点上。
        """
        if handler not in self._listeners:
            self._listeners.append(handler)

    def remove_listener(self, handler: NotificationHandler) -> None:
        try:
            self._listeners.remove(handler)
        except ValueError:
            pass

    def _dispatch_notification(self, payload: dict[str, Any]) -> None:
        for handler in list(self._listeners):
            try:
                handler(payload)
            except Exception:  # pragma: no cover - 订阅者出错不该影响协议线程
                pass

    def _dispatch_stderr(self, line: str) -> None:
        # 晚绑定：界面可能在建好之后才挂上处理器，而已创建的 worker 也要能转发过来。
        handler = self.on_worker_stderr
        if handler is not None:
            try:
                handler(line)
            except Exception:  # pragma: no cover
                pass

    # -- 发现 -----------------------------------------------------------------

    def discover(self, *, strict: bool = False) -> list[Manifest]:
        """扫描插件目录。默认容错：坏插件被记录进 ``problems``，不影响其它插件。"""
        self._records.clear()
        self.problems.clear()
        self._discovered = True

        if not self.plugins_dir.is_dir():
            message = f"插件目录不存在：{self.plugins_dir}"
            if strict:
                raise ManifestError(message)
            self.problems.append((str(self.plugins_dir), message))
            return []

        manifests: list[Manifest] = []
        for entry in sorted(self.plugins_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            if not (entry / MANIFEST_FILENAME).is_file():
                continue
            try:
                manifest = load_manifest(entry)
            except ManifestError as exc:
                if strict:
                    raise
                self.problems.append((entry.name, str(exc)))
                continue
            if manifest.id in self._records:
                message = f"插件 id 重复：{manifest.id}（{entry}）"
                if strict:
                    raise ManifestError(message)
                self.problems.append((entry.name, message))
                continue
            self._records[manifest.id] = PluginRecord(manifest)
            manifests.append(manifest)

        return manifests

    def _ensure_discovered(self) -> None:
        if not self._discovered:
            self.discover()

    # -- 查询 -----------------------------------------------------------------

    @property
    def manifests(self) -> dict[str, Manifest]:
        self._ensure_discovered()
        return {pid: rec.manifest for pid, rec in self._records.items()}

    def manifest(self, plugin_id: str) -> Manifest:
        self._ensure_discovered()
        record = self._records.get(plugin_id)
        if record is None:
            known = ", ".join(sorted(self._records)) or "（无）"
            raise ManifestError(f"找不到插件 {plugin_id!r}。已发现：{known}")
        return record.manifest

    def has(self, plugin_id: str) -> bool:
        self._ensure_discovered()
        return plugin_id in self._records

    # -- 进程与接口 -----------------------------------------------------------

    def client(self, plugin_id: str) -> WorkerClient:
        """拿到插件进程客户端，需要时启动它并完成握手。"""
        self._ensure_discovered()
        record = self._records.get(plugin_id)
        if record is None:
            raise ManifestError(f"找不到插件 {plugin_id!r}")

        if record.load_error:
            raise PluginError(f"插件 {plugin_id} 无法加载：{record.load_error}")

        if record.client is not None and record.client.alive:
            return record.client

        client = WorkerClient(
            record.manifest,
            repo_root=self.repo_root,
            python_exe=self.python_exe,
            on_notification=self._dispatch_notification,
            on_stderr=self._dispatch_stderr,
            default_timeout=self.worker_timeout,
        )
        try:
            client.start()
        except PluginError as exc:
            record.load_error = str(exc)
            raise
        record.client = client
        return client

    def interface(self, plugin_id: str) -> dict[str, Any]:
        """拿到并缓存插件的完整接口描述（会启动它的进程）。"""
        self._ensure_discovered()
        record = self._records.get(plugin_id)
        if record is None:
            raise ManifestError(f"找不到插件 {plugin_id!r}")
        if record.interface is not None:
            return record.interface

        client = self.client(plugin_id)
        try:
            description = client.request("describe", {}, timeout=30.0)
        except PluginError as exc:
            record.load_error = str(exc)
            raise

        self._cross_check(record.manifest, description)
        record.interface = description
        return description

    def _cross_check(self, manifest: Manifest, description: dict[str, Any]) -> None:
        """manifest 的静态声明必须和代码里实际注册的一致。

        不一致的话，界面上会出现"面板里有、点了报错"的节点。宁可在加载阶段就报出来。
        """
        declared_actions = set(manifest.provides.get("actions") or [])
        declared_triggers = set(manifest.provides.get("triggers") or [])
        actual_actions = set(description.get("actions") or {})
        actual_triggers = set(description.get("triggers") or {})

        problems: list[str] = []
        for label, declared, actual in (
            ("action", declared_actions, actual_actions),
            ("trigger", declared_triggers, actual_triggers),
        ):
            missing = declared - actual
            undeclared = actual - declared
            if missing:
                problems.append(
                    f"manifest 声明了 {label} {sorted(missing)}，但代码里没有注册"
                )
            if undeclared and (declared_actions or declared_triggers):
                problems.append(
                    f"代码注册了 {label} {sorted(undeclared)}，但 manifest 的 provides 里没写"
                )
        if problems:
            raise ManifestError(f"插件 {manifest.id} 的 manifest 与实际接口不一致：" + "；".join(problems))

    def node_spec(self, plugin_id: str, action_id: str) -> dict[str, Any] | None:
        """查一个节点类型，返回接口描述（含 inputs/outputs），找不到返回 None。"""
        self._ensure_discovered()
        record = self._records.get(plugin_id)
        if record is None:
            return None
        try:
            self.interface(plugin_id)
        except (ManifestError, PluginError):
            return None
        return record.node_spec(action_id)

    def preload(self, plugin_ids: Iterable[str]) -> list[tuple[str, str]]:
        """提前拉起一批插件进程。返回 [(插件 id, 错误信息)]。

        并行执行时很有用：先一次性把进程都拉起来，第一个节点的启动延迟就不会
        混在运行耗时里。
        """
        failures: list[tuple[str, str]] = []
        for plugin_id in dict.fromkeys(plugin_ids):
            try:
                self.interface(plugin_id)
            except (ManifestError, PluginError) as exc:
                failures.append((plugin_id, str(exc)))
        return failures

    def catalog(self, *, load_interfaces: bool = True) -> list[dict[str, Any]]:
        """给界面用的完整插件目录：左侧模块面板的数据源。"""
        self._ensure_discovered()
        out: list[dict[str, Any]] = []
        for plugin_id, record in sorted(self._records.items()):
            info: dict[str, Any] = {
                **record.manifest.to_dict(),
                "path": str(record.manifest.path),
                "actions": [],
                "triggers": [],
                "loaded": False,
            }
            if load_interfaces and not record.load_error:
                try:
                    self.interface(plugin_id)
                except (ManifestError, PluginError) as exc:
                    record.load_error = str(exc)
            if record.interface:
                info["loaded"] = True
                info["actions"] = list((record.interface.get("actions") or {}).values())
                info["triggers"] = list((record.interface.get("triggers") or {}).values())
            elif record.load_error:
                info["load_error"] = record.load_error

            # 静态声明的动作也要能看到，即使进程拉不起来。
            if not info["actions"] and not info["triggers"]:
                info["actions"] = [
                    {"id": a, "name": a, "kind": "action", "inputs": {}, "outputs": {}}
                    for a in record.manifest.provides.get("actions", [])
                ]
                info["triggers"] = [
                    {"id": t, "name": t, "kind": "trigger", "inputs": {}, "outputs": {}}
                    for t in record.manifest.provides.get("triggers", [])
                ]

            # 依赖检查放在最后：`dependencies` 字段是 manifest 里声明的，界面要靠它
            # 决定"要不要显示安装按钮"。
            #
            # **声明 + 报错两条都看。** 只看声明的话，一个没写 dependencies 的插件撞上
            # ModuleNotFoundError 时，用户会被卡死，而界面上连个按钮都没有 —— 那恰恰是
            # 最常见的情况。
            declared = record.manifest.dependencies
            info["dependencies"] = list(declared)
            info["missing_dependencies"] = suggest(declared, record.load_error or "")
            out.append(info)
        return out

    def loaded_plugin_ids(self) -> list[str]:
        return [pid for pid, rec in self._records.items() if rec.client is not None]

    # -- 自定义模块：识别与删除 ------------------------------------------------

    def is_builtin(self, plugin_id: str) -> bool:
        """是不是随程序一起发出去的插件。

        判定依据是 manifest 里的 ``"builtin": true``。**默认当成"可删"是危险的方向**，
        所以反过来：只有明确标了内置的才保护。这样用户自己手写的插件也天然能被管理到，
        不需要额外登记。
        """
        manifest = self.manifests.get(plugin_id)
        return bool(manifest and manifest.raw.get("builtin"))

    def custom_ids(self) -> list[str]:
        """用户自己的模块 id（可删的那些）。"""
        return sorted(pid for pid in self.manifests if not self.is_builtin(pid))

    def save_source(
        self,
        plugin_id: str,
        *,
        code: str,
        name: str = "",
        description: str = "",
        category: str = "",
    ) -> Path:
        """覆盖已有模块的 ``main.py``，并**就地**更新 manifest 里那几个字段。

        为什么不整份重写 manifest：用户完全可能自己往里加了字段（``keywords``、
        ``homepage``、``runtime.venv``…）。整份重写会把它们无声地抹掉，而用户只改了一行
        代码 —— 那种损失要过很久才会被发现。所以只碰我们负责的那几个键。

        ``id`` 和目录名一律不动，理由同 :meth:`rename`。
        """
        import json  # noqa: PLC0415

        self._ensure_discovered()
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginError(f"没有这个插件：{plugin_id}")
        if record.manifest.raw.get("builtin"):
            raise PluginError(f"{plugin_id} 是内置模块，不能改代码")

        directory = Path(record.manifest.path)
        (directory / record.manifest.runtime.entry).write_text(code, encoding="utf-8")

        path = directory / MANIFEST_FILENAME
        data = json.loads(path.read_text(encoding="utf-8"))
        if name.strip():
            data["name"] = name.strip()
        if description.strip():
            data["description"] = description.strip()
        if category.strip():
            data["category"] = category.strip()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        record.manifest.name = str(data.get("name") or plugin_id)
        record.manifest.description = str(data.get("description") or "")
        record.manifest.category = str(data.get("category") or "通用")
        record.manifest.raw.update(data)
        return directory

    def rename(self, plugin_id: str, new_name: str) -> Path:
        """改模块的**显示名**，返回 manifest 路径。

        只动 ``manifest.json`` 里的 ``name``，**目录名和 id 都不动**。这不是偷懒：
        id 是流程文件引用插件的方式，改了它，所有用到这个模块的流程会立刻全部失效，
        而且用户看到的现象是"流程莫名其妙打不开了"。想换 id 只有一个安全的做法 ——
        新建一个模块，把流程一个个搬过去。
        """
        import json  # noqa: PLC0415 - 只在改名时用

        self._ensure_discovered()
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginError(f"没有这个插件：{plugin_id}")
        if record.manifest.raw.get("builtin"):
            raise PluginError(f"{plugin_id} 是内置模块，不能改名")

        name = new_name.strip()
        if not name:
            raise PluginError("模块名不能为空")

        path = Path(record.manifest.path) / MANIFEST_FILENAME
        data = json.loads(path.read_text(encoding="utf-8"))
        data["name"] = name
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        # 就地更新内存里的那份，免得再扫一遍目录（扫描会重新拉起所有插件进程）。
        record.manifest.name = name
        record.manifest.raw["name"] = name
        return path

    def remove(self, plugin_id: str, *, force: bool = False) -> Path:
        """停掉插件进程，把它的整个目录**删到回收站**，返回被删的路径。

        顺序不能反：**必须先停进程**。Windows 上只要还有进程占着目录里的文件
        （``__pycache__`` 尤其容易被占），删除就会失败。

        ``force=True`` 才允许删内置插件。默认拦住是**故意的**：流程按 id 引用插件，删掉一个
        内置的会让用到它的流程全部失效。界面在明确告诉用户"哪些流程会受影响"之后才传它 ——
        保护留在这一层，而不是靠调用方自觉。
        """
        self._ensure_discovered()
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginError(f"没有这个插件：{plugin_id}")
        if record.manifest.raw.get("builtin") and not force:
            raise PluginError(f"{plugin_id} 是内置插件，删除需要显式确认")

        target = Path(record.manifest.path)
        if record.client is not None:
            try:
                record.client.stop()
            except Exception:
                record.client.kill()
            record.client = None

        # 只删插件目录本身，绝不递归到 plugins/ 外面去 —— 一个写错的 manifest 不该
        # 有机会删掉用户别的东西。
        root = self.plugins_dir.resolve()
        resolved = target.resolve()
        if resolved == root or root not in resolved.parents:
            raise PluginError(f"拒绝删除 {target}：它不在插件目录 {root} 里面")

        # **先删磁盘、再从注册表里摘。** 反过来（原实现）删失败时会留下一个"注册表里没有、
        # 目录还在"的幽灵，用户看到模块从界面上消失但文件还在，最难受的是没法再删一次。
        try:
            to_recycle_bin(resolved)
        except Exception as exc:
            raise PluginError(f"删不掉 {plugin_id}：{exc}") from exc

        self._records.pop(plugin_id, None)
        return resolved

    # -- 收尾 -----------------------------------------------------------------

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for record in self._records.values():
            if record.client is not None:
                try:
                    record.client.stop()
                except Exception:
                    record.client.kill()
                record.client = None

    def __enter__(self) -> "Registry":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.shutdown()

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"<Registry {self.plugins_dir} plugins={len(self._records)}>"
