"""工作流库：把磁盘上的工作流文件列成一张可供界面展示的表。

界面首页要显示"有哪些自动化流程"，每一条需要：名称、描述、节点数、**上次运行结果**。
前三个来自工作流文件本身，最后一个来自运行历史库 —— 这里把两边接起来。

不放在 ``host`` 里是因为它跟 Qt 无关：命令行工具同样需要"列出所有流程"，
测试也可以直接验证它，不用起界面。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable

from .errors import MyAutoWorkError
from .graph import load_workflow
from .recycle import to_recycle_bin

__all__ = ["WorkflowEntry", "WorkflowLibrary"]

#: 扫描工作流目录时跳过的名字。
_SKIP_PREFIXES = (".", "_")


@dataclass
class WorkflowEntry:
    """工作流库里的一个条目。"""

    path: Path
    id: str = ""
    name: str = ""
    description: str = ""
    node_count: int = 0
    edge_count: int = 0
    modified_at: float = 0.0
    #: 文件存在但解析失败时的原因。界面要能把它显示出来，而不是整页报错。
    error: str | None = None
    #: 最近一次运行记录（来自 RunStore），没有则为空。
    last_run: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def display_name(self) -> str:
        return self.name or self.path.stem

    @property
    def last_status(self) -> str:
        return str(self.last_run.get("status") or "")

    @property
    def last_started_at(self) -> str:
        return str(self.last_run.get("started_at") or "")

    @property
    def last_duration_ms(self) -> int:
        value = self.last_run.get("duration_ms")
        return int(value) if isinstance(value, (int, float)) else 0

    @property
    def modified_text(self) -> str:
        if not self.modified_at:
            return ""
        import datetime as _dt

        return _dt.datetime.fromtimestamp(self.modified_at).strftime("%Y-%m-%d %H:%M")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "id": self.id,
            "name": self.display_name,
            "description": self.description,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "error": self.error,
            "last_run": dict(self.last_run),
        }


class WorkflowLibrary:
    """扫描一个目录，把里面的工作流文件读成条目列表。"""

    def __init__(self, root: str | Path, store: Any = None) -> None:
        self.root = Path(root)
        self.store = store
        self.entries: list[WorkflowEntry] = []
        #: 解析失败的文件，形如 [(路径, 原因)]。
        self.problems: list[tuple[str, str]] = []

    # -- 扫描 ----------------------------------------------------------------

    def discover(self, *, recursive: bool = True) -> list[WorkflowEntry]:
        self.entries = []
        self.problems = []

        if not self.root.is_dir():
            return self.entries

        pattern = "**/*.json" if recursive else "*.json"
        last_runs = self._last_runs()

        for path in sorted(self.root.glob(pattern)):
            if not path.is_file() or path.name.startswith(_SKIP_PREFIXES):
                continue
            entry = self._read(path)
            entry.last_run = last_runs.get(entry.id, {})
            self.entries.append(entry)

        # 能跑的排前面、最近改动的更靠前。坏文件仍然显示，但不该占着第一屏的位置 ——
        # 用户打开软件是想找一条能跑的东西，不是想看哪条坏了。
        self.entries.sort(key=lambda item: (not item.ok, -item.modified_at, item.display_name))
        return self.entries

    def _read(self, path: Path) -> WorkflowEntry:
        try:
            stat = path.stat()
            modified_at = stat.st_mtime
        except OSError:
            modified_at = 0.0

        try:
            workflow = load_workflow(path)
        except MyAutoWorkError as exc:
            self.problems.append((str(path), str(exc)))
            return WorkflowEntry(
                path=path,
                id=path.stem,
                name=path.stem,
                modified_at=modified_at,
                error=str(exc),
            )

        return WorkflowEntry(
            path=path,
            id=workflow.id,
            name=workflow.name,
            description=workflow.description,
            node_count=len(workflow.nodes),
            edge_count=len(workflow.edges),
            modified_at=modified_at,
        )

    def _last_runs(self) -> dict[str, dict[str, Any]]:
        if self.store is None:
            return {}
        try:
            return self.store.latest_runs_by_workflow()
        except Exception:  # pragma: no cover - 历史库损坏不该让首页打不开
            return {}

    # -- 查询 ----------------------------------------------------------------

    def find(self, path: str | Path) -> WorkflowEntry | None:
        target = Path(path)
        for entry in self.entries:
            if entry.path == target:
                return entry
        return None

    def refresh_entry(self, path: str | Path) -> WorkflowEntry | None:
        """只重读一个文件，避免整个首页闪一下。"""
        target = Path(path)
        for index, entry in enumerate(self.entries):
            if entry.path == target:
                fresh = self._read(target)
                fresh.last_run = (self._last_runs()).get(fresh.id, {})
                self.entries[index] = fresh
                return fresh
        return None

    def remove(self, path: str | Path) -> Path:
        """把一个流程文件**删到回收站**，返回删掉的路径。

        走回收站是因为流程是用户自己写的东西 —— 界面上多一个按钮，误点的代价不该是
        "永久没了"。非 Windows 上会退回永久删除（``to_recycle_bin`` 的返回值会说明）。

        **只删工作流目录里的文件。** 路径再往下走一层都不行 —— 删除这种事，判据宁可窄。
        """
        target = Path(path).resolve()
        root = Path(self.root).resolve()
        if target == root or root not in target.parents:
            raise MyAutoWorkError(f"拒绝删除 {target}：它不在流程目录 {root} 里面")
        if not target.is_file():
            raise MyAutoWorkError(f"要删的流程不存在：{target}")

        to_recycle_bin(target)

        # 删完立刻从列表里摘掉。留着的话界面会显示一条点不动的幽灵。
        self.entries = [e for e in self.entries if Path(e.path).resolve() != target]
        return target

    def filtered(self, keyword: str) -> list[WorkflowEntry]:
        needle = (keyword or "").strip().lower()
        if not needle:
            return list(self.entries)
        out = []
        for entry in self.entries:
            haystack = f"{entry.display_name} {entry.description} {entry.path.name}".lower()
            if needle in haystack:
                out.append(entry)
        return out

    def __iter__(self) -> Iterable[WorkflowEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def ensure_directory(path: str | Path) -> Path:
    target = Path(path)
    os.makedirs(target, exist_ok=True)
    return target
