"""运行历史存储（SQLite）。

为什么第 1 步就要有它？

因为自动化绝大多数时候是**无人值守**跑的。半年后你发现某天导出的报表少了一行，
唯一能回答"当时到底发生了什么"的就是这份历史：哪个节点、几点、耗时多少、返回
了什么、报了什么错。等到出问题才想起来要加日志，那段历史就永远没有了。

**线程说明**：图形界面会在 QThread 里跑工作流，而历史面板在主线程里读。所以连接
必须允许跨线程（``check_same_thread=False``），并且所有访问都用一把锁串起来 ——
sqlite3 的连接对象本身不是线程安全的。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

__all__ = ["RunStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    workflow_id   TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    duration_ms   INTEGER,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS node_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    plugin      TEXT NOT NULL,
    action      TEXT NOT NULL,
    title       TEXT,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    outputs     TEXT,
    error       TEXT,
    logs        TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_node_runs_run ON node_runs(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
"""


class RunStore:
    """运行历史的读写。线程安全。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- 写入 -----------------------------------------------------------------

    def start_run(
        self,
        *,
        run_id: str,
        workflow_id: str,
        workflow_name: str,
        started_at: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs "
                "(run_id, workflow_id, workflow_name, status, started_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, workflow_id, workflow_name, "running", started_at),
            )
            self._conn.commit()

    def finish_run(
        self,
        *,
        run_id: str,
        status: str,
        finished_at: str,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = ?, finished_at = ?, duration_ms = ?, error = ? "
                "WHERE run_id = ?",
                (status, finished_at, duration_ms, error, run_id),
            )
            self._conn.commit()

    def record_nodes(self, run_id: str, results: Iterable[Any], *, start_seq: int = 0) -> None:
        """写入节点执行记录。``results`` 是 engine.NodeResult 序列。

        支持逐条写入（``start_seq`` 由调用方递增），这样长流程跑到一半崩掉时，
        已完成节点的记录仍然在库里。
        """
        rows = []
        for seq, result in enumerate(results, start=start_seq):
            rows.append(
                (
                    run_id,
                    result.node_id,
                    seq,
                    result.plugin,
                    result.action,
                    result.title,
                    result.status,
                    result.started_at,
                    result.duration_ms,
                    _dump(result.outputs),
                    result.error,
                    _dump(result.logs),
                )
            )
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO node_runs "
                "(run_id, node_id, seq, plugin, action, title, status, started_at, "
                " duration_ms, outputs, error, logs) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    # -- 读取 -----------------------------------------------------------------

    def recent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def latest_runs_by_workflow(self) -> dict[str, dict[str, Any]]:
        """每条工作流最近一次运行，返回 ``{workflow_id: run}``。

        首页的流程列表要显示"上次运行结果"，但不可能为每张卡片查一次库。
        一条 SQL 全部取回来，界面上就是一次内存查询。
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT r.* FROM runs r "
                "JOIN (SELECT workflow_id, MAX(started_at) AS newest "
                "      FROM runs GROUP BY workflow_id) t "
                "  ON r.workflow_id = t.workflow_id AND r.started_at = t.newest"
            )
            return {str(row["workflow_id"]): dict(row) for row in cursor.fetchall()}

    def last_run_for(self, workflow_id: str) -> dict[str, Any]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM runs WHERE workflow_id = ? ORDER BY started_at DESC LIMIT 1",
                (workflow_id,),
            )
            row = cursor.fetchone()
        return dict(row) if row is not None else {}

    def run_nodes(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM node_runs WHERE run_id = ? ORDER BY seq", (run_id,)
            )
            rows = [dict(row) for row in cursor.fetchall()]
        for item in rows:
            item["outputs"] = _load(item.get("outputs"))
            item["logs"] = _load(item.get("logs")) or []
        return rows

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover
                pass

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _load(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw
