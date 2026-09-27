"""在后台跑 ``pip install``，把输出一行行流回界面。

**为什么是子进程而不是 ``pip.main()``。** 在同一个进程里跑 pip 会污染当前解释器的
``sys.path`` 和已导入模块的状态 —— 装完之后那些模块还是旧版本，直到重启。而且 pip 在
进程内跑不好取消。子进程两头都干净。

**为什么要做成 QThread 而不是 subprocess 加定时器。** 装一个大包要几十秒。跑在主线程里
界面会整个冻住，连"取消"都点不了 —— 这正是 runner.py 里那条教训。

**取消 = 杀进程。** pip 自己不支持中途优雅退出；杀掉是唯一能立刻停下来的方式，代价是可能
留下半装的状态。所以取消之后界面上会提示"建议重新安装一次"，而不是假装什么都没发生。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

__all__ = ["DependencyInstallThread"]


class DependencyInstallThread(QThread):
    """跑一次 ``pip install``。"""

    #: pip 的每一行输出
    line = Signal(str)
    #: (是否成功, 一句话结论)
    finished_with = Signal(bool, str)

    def __init__(
        self,
        python_exe: str,
        requirements: list[str],
        parent: Any = None,
        extra_index: str = "",
    ) -> None:
        super().__init__(parent)
        self.python_exe = str(python_exe)
        self.requirements = list(requirements)
        self.extra_index = extra_index
        self._proc: subprocess.Popen[str] | None = None
        self._cancelled = False

    def command(self) -> list[str]:
        cmd = [
            self.python_exe,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
        ]
        if self.extra_index:
            cmd += ["-i", self.extra_index]
        cmd += self.requirements
        return cmd

    def run(self) -> None:  # noqa: D102 - QThread 重写
        cmd = self.command()
        self.line.emit("$ " + " ".join(cmd))

        # Windows 上不加这个，pip 会弹一个黑框出来闪一下。
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=flags,
            )
        except OSError as exc:
            self.finished_with.emit(False, f"起不了 pip（{self.python_exe}）：{exc}")
            return

        stream = self._proc.stdout
        if stream is not None:
            for raw in stream:
                self.line.emit(raw.rstrip())
                if self._cancelled:
                    break

        code = self._proc.wait()
        if self._cancelled:
            self.finished_with.emit(False, "已取消 —— 可能装了一半，建议重新安装一次")
        elif code == 0:
            self.finished_with.emit(True, "依赖装好了")
        else:
            self.finished_with.emit(False, f"pip 退出码 {code}")

    def cancel(self) -> None:
        self._cancelled = True
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()


def target_python(manifest: Any, fallback: str) -> str:
    """该往哪个解释器里装：跟着"这个插件跑起来会用哪个解释器"走。

    不能想当然用主 venv —— 插件自带 venv 的时候，装到主 venv 里等于没装。
    """
    if manifest is None:
        return fallback
    return manifest.python_executable(fallback)


def describe_target(python_exe: str) -> str:
    """给界面显示一句"装到哪去了"。"""
    path = Path(python_exe)
    for parent in path.parents:
        if (parent / "pyvenv.cfg").is_file():
            return f"虚拟环境 {parent}"
    return f"解释器 {python_exe}"
