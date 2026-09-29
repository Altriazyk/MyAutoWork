"""内核侧的插件进程客户端：stdio 上的 JSON-RPC。

协议选择 stdio 而不是本地 socket / 命名管道的理由：

1. 不占端口、不触发防火墙弹窗（Windows 上这点很实际）
2. 进程一死管道立刻 EOF，健康检查是免费的、不需要心跳
3. 生命周期天然绑定：内核退出，子进程的 stdin 关闭，插件自己就退了

两条硬规则：

- **插件进程的 stdout 是协议通道**，任何 ``print`` 都会污染它。所以 worker 侧会把
  fd 1 重定向到 stderr，插件里 print 会安全地变成日志。
- **超时必须杀进程，不能只放弃等待**。一个卡住的界面自动化插件会继续持有窗口
  句柄、继续点鼠标，放弃等待等于放任它在后台乱来。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import Future, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any, Callable

from .errors import PluginError, WorkerTimeout
from .manifest import Manifest

__all__ = ["WorkerClient"]

NotificationHandler = Callable[[dict[str, Any]], None]
StderrHandler = Callable[[str], None]


#: 传给 ``request(timeout=...)`` 表示**不限时**。
#:
#: 它和 ``None`` 不是一回事：``None`` 是"没指定，用客户端的默认值"，这个才是
#: "一直等下去"。用一个负数当标记 —— 负的超时本来就毫无意义，不会和真实值撞上。
FOREVER = -1.0


class WorkerClient:
    """管理一个插件 worker 进程的完整生命周期。"""

    def __init__(
        self,
        manifest: Manifest,
        *,
        repo_root: str | Path,
        python_exe: str | None = None,
        on_notification: NotificationHandler | None = None,
        on_stderr: StderrHandler | None = None,
        default_timeout: float = 300.0,
    ) -> None:
        self.manifest = manifest
        self.repo_root = Path(repo_root)
        self.python_exe = manifest.python_executable(python_exe or sys.executable)
        self.default_timeout = default_timeout

        self._on_notification = on_notification
        self._on_stderr = on_stderr

        self._proc: subprocess.Popen[str] | None = None
        self._pending: dict[int, Future[dict[str, Any]]] = {}
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 0
        self._alive = False
        self._threads: list[threading.Thread] = []

    # -- 生命周期 -------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._alive and self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """拉起插件进程。重复调用是幂等的。"""
        if self.alive:
            return

        env = os.environ.copy()
        # 插件要能 import myautowork（仓库根）和它自己的同级模块（插件目录）。
        parts = [str(self.repo_root), str(self.manifest.path), env.get("PYTHONPATH", "")]
        env["PYTHONPATH"] = os.pathsep.join(p for p in parts if p)
        # 全程 UTF-8，避免 Windows 默认 cp936 把中文日志/路径搞坏。
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        cmd = [
            self.python_exe,
            "-m",
            "kernel.worker",
            "--plugin-dir",
            str(self.manifest.path),
        ]

        creationflags = 0
        if os.name == "nt":  # pragma: no cover - 平台分支
            # 插件进程不应该弹出控制台窗口。
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(self.manifest.path),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise PluginError(
                f"无法启动插件 {self.manifest.id} 的进程：{exc}（解释器 {self.python_exe}）"
            ) from exc

        self._alive = True
        self._threads = [
            threading.Thread(
                target=self._read_stdout, name=f"rpc-{self.manifest.id}", daemon=True
            ),
            threading.Thread(
                target=self._read_stderr, name=f"err-{self.manifest.id}", daemon=True
            ),
        ]
        for thread in self._threads:
            thread.start()

        # 握手：确认插件加载成功、装饰器没写错。
        # 放在这里而不是第一次 invoke 时，是为了让"写错的插件"在启动阶段就暴露。
        self.request("ping", {}, timeout=30.0)

    def stop(self, *, timeout: float = 5.0) -> None:
        """优雅退出；不配合就强杀。"""
        proc = self._proc
        if proc is None:
            return

        if proc.poll() is None:
            try:
                self.request("shutdown", {}, timeout=timeout)
            except Exception:
                pass  # 插件不配合是常态，下面直接杀

        self._alive = False
        if proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.kill()
        self._proc = None
        self._fail_pending(PluginError(f"插件 {self.manifest.id} 已停止"))

    def kill(self) -> None:
        """强杀。超时路径专用。"""
        proc = self._proc
        self._alive = False
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def restart(self) -> None:
        """崩了之后重新拉起，让工作流能继续往下跑。"""
        self.kill()
        self._proc = None
        self.start()

    # -- 调用 -----------------------------------------------------------------

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """发一次 RPC 并等待响应。"""
        if not self.alive:
            raise PluginError(f"插件 {self.manifest.id} 的进程未运行")

        proc = self._proc
        assert proc is not None and proc.stdin is not None

        with self._state_lock:
            self._next_id += 1
            request_id = self._next_id
            future: Future[dict[str, Any]] = Future()
            self._pending[request_id] = future

        payload = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
            ensure_ascii=False,
        )

        try:
            with self._write_lock:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._pending.pop(request_id, None)
            raise PluginError(
                f"插件 {self.manifest.id} 的进程已断开，无法发送 {method} 请求"
            ) from exc

        # **``None`` 和 ``FOREVER`` 不是一回事。**
        # ``timeout=None`` 是"没指定，用客户端的默认值"；``FOREVER`` 才是"真的等下去"。
        if timeout is None:
            effective_timeout: float | None = self.default_timeout
        elif timeout < 0:
            # ``Future.result(timeout=None)`` 会一直阻塞，底层不加任何计时器 ——
            # 这才是真正的"不限时"。
            #
            # 以前这里想用"一个足够大的秒数"糊过去，结果那个数（31 亿秒）把
            # ``lock.acquire`` 的毫秒参数撑爆了，报 ``OverflowError: timeout value
            # is too large``。**大数不等于无限**：底层到处都有位宽限制。
            effective_timeout = None
        else:
            effective_timeout = timeout
        try:
            message = future.result(timeout=effective_timeout)
        except FuturesTimeout:
            self._pending.pop(request_id, None)
            # 必须杀掉：卡住的自动化插件会在后台继续操作界面。
            self.kill()
            raise WorkerTimeout(
                f"插件 {self.manifest.id} 在 {effective_timeout:g}s 内没有响应，进程已终止"
            )

        if "error" in message and message["error"]:
            error = message["error"]
            if isinstance(error, dict):
                raise PluginError(error.get("message", "插件返回了未知错误"), detail=error)
            raise PluginError(str(error))
        result = message.get("result")
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise PluginError(f"插件 {self.manifest.id} 的 {method} 返回了非对象结果")
        return result

    # -- 接收 -----------------------------------------------------------------

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    # 协议被污染（多半是插件里直接 print 了）。不静默丢弃。
                    self._emit_stderr(f"[{self.manifest.id}] 非协议输出被忽略：{line[:300]}")
                    continue

                if message.get("method") == "notify":
                    self._handle_notification(message.get("params") or {})
                    continue

                request_id = message.get("id")
                if request_id is None:
                    continue
                with self._state_lock:
                    future = self._pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(message)
        except Exception as exc:  # pragma: no cover - 读取线程兜底
            self._emit_stderr(f"[{self.manifest.id}] 读取线程异常：{exc}")
        finally:
            self._on_eof()

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.rstrip()
                if line:
                    self._emit_stderr(line)
        except Exception:  # pragma: no cover - 进程退出时正常发生
            pass

    def _handle_notification(self, params: dict[str, Any]) -> None:
        if self._on_notification is not None:
            try:
                self._on_notification({"plugin": self.manifest.id, **params})
            except Exception:  # pragma: no cover - 回调不该影响协议线程
                pass

    def _emit_stderr(self, line: str) -> None:
        if self._on_stderr is not None:
            try:
                self._on_stderr(line)
            except Exception:  # pragma: no cover
                pass

    def _on_eof(self) -> None:
        """进程退出。把所有还在等的调用一次性失败掉，避免它们一直挂到超时。"""
        if not self._alive:
            return
        self._alive = False
        proc = self._proc
        code = proc.poll() if proc is not None else None
        self._fail_pending(
            PluginError(
                f"插件 {self.manifest.id} 的进程意外退出"
                + (f"（退出码 {code}）" if code is not None else "")
            )
        )

    def _fail_pending(self, exc: Exception) -> None:
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(exc)

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        state = "alive" if self.alive else "stopped"
        return f"<WorkerClient {self.manifest.id} {state}>"
