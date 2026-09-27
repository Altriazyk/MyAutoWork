"""启动和关闭电脑上的程序。

自动化开始之前的那一步常常就是"先把某个软件打开"（模拟器、微信、浏览器）。没有这个动作，
那一整条流程只能靠人手动点第一下 —— 这也是 `win.window` 那组动作的盲区：它们全都作用于
**已经存在的窗口**，程序没起来就无从下手。

**启动之后必须彻底断开。** ``DETACHED_PROCESS`` 加三个 ``DEVNULL`` 不是可选项：插件的宿主
进程是通过管道读 worker 的 stdout 的，如果被启动的程序**继承了那根管道**，宿主会一直等它
关闭 —— 表现就是"流程卡在这儿不动了，直到你手动关掉那个程序"。Windows 上子孙进程继承句柄
是默认行为，所以这里必须显式切断。

**"等窗口出现"比"等 N 秒"靠谱。** 模拟器冷启动可能 8 秒也可能 40 秒，机器快慢、开了几个
实例都会影响。写死 sleep 要么白等要么不够。真正的信号是那个窗口出现了。
"""

from __future__ import annotations

import csv
import ctypes
import io
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from myautowork import (
    Bool,
    Context,
    File,
    Folder,
    Integer,
    Number,
    String,
    action,
)

__all__ = ["launch", "kill", "running", "open_with"]

_IS_WINDOWS = sys.platform == "win32"

#: 不带控制台窗口（tasklist / taskkill 用，免得闪一下黑框）。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0

#: 新进程不继承本进程的控制台和标准句柄。理由见模块开头。
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) if _IS_WINDOWS else 0


def _split_args(text: str) -> list[str]:
    """按空格拆参数，双引号里的空格不算分隔。

    **不用 shlex。** posix 模式会把 Windows 路径里的反斜杠当转义符吃掉 ——
    ``C:\\Program Files\\x`` 会变成 ``C:Program Filesx``。这个坑很隐蔽，所以自己拆。
    """
    out: list[str] = []
    current: list[str] = []
    in_quotes = False
    for char in text or "":
        if char == '"':
            in_quotes = not in_quotes
            continue
        if char.isspace() and not in_quotes:
            if current:
                out.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        out.append("".join(current))
    return out


def _normalize_name(name: str) -> str:
    """``MEmu`` -> ``MEmu.exe``。少了后缀 tasklist 一个都匹配不上，而它不会报错。"""
    name = (name or "").strip()
    if name and "." not in name:
        return name + ".exe"
    return name


def _find_windows(title_part: str) -> list[tuple[int, str]]:
    """标题里含 ``title_part`` 的可见窗口，``[(句柄, 标题)]``。

    **空字符串表示"所有可见窗口"** —— 等窗口超时时要把实际出现的标题列给用户看，
    否则猜错标题的人只会看到一句"没等到"，无从下手。
    """
    if not _IS_WINDOWS:
        return []

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []
    needle = (title_part or "").lower()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)  # type: ignore[attr-defined]
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if not needle or needle in title.lower():
            found.append((int(hwnd), title))
        return True

    user32.EnumWindows(callback, 0)
    return found


def _process_pids(name: str) -> list[int]:
    """按进程名查 PID。用 tasklist，不为这一件事引入 psutil。"""
    if not _IS_WINDOWS:
        return []
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    pids: list[int] = []
    # 用 csv 而不是 split(",")：机器名/内存字段里可能有逗号，而且"没找到"时 tasklist
    # 返回的是一句本地化提示（"信息: 没有运行的任务…"），csv 会把它当成一行普通文本，
    # 正好不会误判成进程。
    for row in csv.reader(io.StringIO(result.stdout or "")):
        if len(row) >= 2 and row[0].strip().lower() == name.lower():
            try:
                pids.append(int(row[1]))
            except ValueError:
                continue
    return pids


@action(
    id="launch",
    name="启动程序",
    category="系统",
    icon="play",
    description="启动一个 exe，并且可以等它的窗口出现。程序会独立运行，流程结束后它继续留着",
    inputs={
        "path": File(
            picker="open",
            required=True,
            label="程序",
            help="要启动的 exe。快捷方式 (.lnk) 也能启动",
        ),
        "args": String(
            default="",
            label="参数",
            help='命令行参数。带空格的用双引号包起来，比如 -i "C:\\我的 目录"',
        ),
        "workdir": Folder(
            default="",
            label="工作目录",
            help="留空就用程序自己所在的目录。有些程序在别处启动会找不到自己的资源",
        ),
        "window_title": String(
            default="",
            label="等窗口标题包含",
            help="出现含这段文字的窗口就算启动好了。**比死等秒数靠谱**",
        ),
        "wait": Number(
            default=0.0,
            label="最多等几秒",
            help="等窗口时的超时。0 = 不等待，启动了就往下走",
        ),
    },
    outputs={
        "pid": Integer(label="进程 PID"),
        "window": String(label="等到的窗口标题"),
        "waited": Number(label="实际等了多久"),
        "args": String(label="拆出来的参数"),
    },
)
def launch(
    ctx: Context,
    path: str,
    args: str = "",
    workdir: str = "",
    window_title: str = "",
    wait: float = 0.0,
    **_: Any,
) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"找不到这个程序：{path}")

    argv = [str(target)] + _split_args(args)
    cwd = workdir or str(target.parent)

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_DETACHED,
            close_fds=True,
        )
    except OSError as exc:
        # 最常见的两种：需要管理员权限（被系统拒绝），或者 exe 架构不对。
        raise RuntimeError(
            f"启动不了 {target.name}：{exc}\n"
            "需要管理员权限的程序，普通权限是启动不了的（而且系统不一定给得出明确提示）。"
        ) from exc

    ctx.info(f"已启动 {target.name}（PID {proc.pid}），工作目录 {cwd}")

    # **这里刻意不登记清理钩子。** 看起来"启动了程序，卸载时该把它关掉"很合理，
    # 但那是错的：这个动作起的是用户要用的东西（模拟器、编辑器、游戏），
    # DETACHED_PROCESS 就是为了让它活过流程。登记 terminate 的话，用户关掉本软件时
    # 那些程序会被一起杀掉 —— 那正好毁掉这个动作存在的意义。
    #
    # 真要"跑完就关"，是**流程自己的事**：在后面接一个「关闭程序」节点。
    # 那让"关"这一步在流程上看得见，而不是藏在插件卸载里。

    title = ""
    waited = 0.0
    started = time.monotonic()

    if window_title and wait > 0:
        deadline = started + wait
        while True:
            hits = _find_windows(window_title)
            if hits:
                title = hits[0][1]
                break
            if time.monotonic() >= deadline:
                # 光说"没等到"没用 —— 猜错标题的人需要看到**实际出现的标题长什么样**。
                # 窗口标题跟系统语言、软件版本都有关（同一个记事本，中文系统上可能是
                # "无标题 - Notepad"），没法预设，只能让他照着抄。
                visible = [t for _h, t in _find_windows("")][:12]
                hint = (
                    "\n当前开着的窗口有：\n"
                    + "\n".join(f"    {t}" for t in visible)
                    + "\n照着上面能对上的那段填进「等窗口标题包含」"
                    if visible
                    else "\n当前一个可见窗口都没有 —— 那多半是启动被系统拒绝了。"
                )
                ctx.warning(
                    f"等了 {wait:.1f} 秒还没看到标题含「{window_title}」的窗口。{hint}"
                )
                break
            time.sleep(0.4)
        waited = time.monotonic() - started
        if title:
            ctx.info(f"窗口出现了：{title}（等了 {waited:.1f} 秒）")
    elif wait > 0:
        # 只给了秒数没给标题 —— 那就老实等，虽然不如等窗口准。
        time.sleep(wait)
        waited = time.monotonic() - started

    return {
        "pid": proc.pid,
        "window": title,
        "waited": round(waited, 2),
        "args": " ".join(argv[1:]),
    }


@action(
    id="kill",
    name="关闭程序",
    category="系统",
    icon="close",
    description="按 PID 或进程名结束程序。默认连子进程一起关 —— 模拟器这类程序关不干净会留下半死状态",
    inputs={
        "pid": Integer(default=0, label="PID", help="从「启动程序」的输出连过来，或者手填"),
        "name": String(
            default="",
            label="进程名",
            help="比如 MEmu.exe。不写 .exe 会自动补上。填了就关掉所有同名的",
        ),
        "force": Bool(default=True, label="强制结束", help="关掉就是杀进程，不给它保存的机会"),
    },
    outputs={"killed": Bool(label="关掉了吗"), "count": Integer(label="关了几个")},
)
def kill(
    ctx: Context,
    pid: int = 0,
    name: str = "",
    force: bool = True,
    **_: Any,
) -> dict[str, Any]:
    if pid:
        # /T 连子进程一起 —— 模拟器主进程死了但 qemu 还挂着的状态很难看。
        cmd = ["taskkill", "/PID", str(int(pid)), "/T"]
        label = f"PID {pid}"
    elif name.strip():
        resolved = _normalize_name(name)
        cmd = ["taskkill", "/IM", resolved, "/T"]
        label = resolved
    else:
        raise ValueError("要么给 PID，要么给进程名（比如 MEmu.exe）")

    if force:
        cmd.append("/F")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"taskkill 跑不起来：{exc}") from exc

    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    if result.returncode != 0:
        # "没找到进程"不是错误 —— 用户要的结果（它不在了）已经达成。
        ctx.warning(f"{label} 没关掉（可能本来就没在跑）：{output}")
        return {"killed": False, "count": 0}

    # taskkill 的中文输出里会带 "PID 1234" 这种片段，数一下关了几个。
    count = output.count("PID ") or (1 if result.returncode == 0 else 0)
    ctx.info(f"已关闭 {label}（{count} 个进程）")
    return {"killed": True, "count": count}


@action(
    id="running",
    name="程序在跑吗",
    category="系统",
    icon="activity",
    description="查某个进程在不在。也用来做「先关掉再启动」—— 避免重复开出来一堆",
    inputs={
        "name": String(
            required=True,
            label="进程名",
            help="比如 MEmu.exe。不写 .exe 会自动补上",
        )
    },
    outputs={
        "running": Bool(label="在跑吗"),
        "count": Integer(label="有几个"),
        "pids": String(label="PID，逗号分隔"),
    },
)
def running(ctx: Context, name: str, **_: Any) -> dict[str, Any]:
    resolved = _normalize_name(name)
    if not resolved:
        raise ValueError("要给一个进程名，比如 MEmu.exe")

    pids = _process_pids(resolved)
    if pids:
        ctx.info(f"{resolved} 正在跑，{len(pids)} 个：{', '.join(str(p) for p in pids)}")
    else:
        ctx.info(f"{resolved} 没在跑")
    return {
        "running": bool(pids),
        "count": len(pids),
        "pids": ",".join(str(p) for p in pids),
    }


@action(
    id="open_with",
    name="用默认程序打开",
    category="系统",
    icon="external-link",
    description="像双击一样打开一个文件、文件夹或网址 —— 用系统认的默认程序，不用指定 exe",
    inputs={
        "target": String(
            required=True,
            label="打开什么",
            help="文件路径、文件夹路径，或者网址（https://…）。用系统默认关联的程序打开",
        )
    },
    outputs={"target": String(label="打开的东西")},
)
def open_with(ctx: Context, target: str, **_: Any) -> dict[str, Any]:
    value = (target or "").strip()
    if not value:
        raise ValueError("要填一个文件、文件夹或网址")

    if not _IS_WINDOWS:
        raise RuntimeError("「用默认程序打开」目前只在 Windows 上实现")

    try:
        os.startfile(value)  # type: ignore[attr-defined]
    except OSError as exc:
        raise RuntimeError(
            f"打不开 {value}：{exc}\n"
            "文件不存在、或者这个类型没有关联任何程序时会这样。"
        ) from exc

    ctx.info(f"已用默认程序打开 {value}")
    return {"target": value}
