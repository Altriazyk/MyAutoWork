"""开机自动运行一条清单。

**怎么实现的。** 在 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` 下写一个启动项。
用 HKCU 而不是 HKLM，是因为它**不需要管理员权限** —— 一个自动化工具为了让自己开机启动而
要求提权，本身就是件很可疑的事。

**为什么生成一个 .pyw 引导文件，而不是把命令直接塞进注册表。** 要跑的命令里有 Python 代码
（加路径、调 kernel.run），塞进注册表值里就是一层套一层的引号转义，改一个字符就悄悄坏掉，
而且用户在注册表里根本看不懂自己在启动什么。生成一个文件之后：注册表里只有
``"<pythonw>" "<那个文件>"``，用户双击就能看到里面是什么、也能直接删。

``.pyw`` + ``pythonw.exe`` = **不弹黑框**。启动项用控制台程序的话，每次开机都会闪一个黑窗口。

**非 Windows 上直接说"不支持"**，不要假装成功 —— 用户会以为设好了，然后一直等一个永远
不会发生的运行。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from kernel.checklist import safe_slug

__all__ = [
    "StartupUnavailable",
    "available",
    "bootstrap_path",
    "launch_command",
    "set_startup",
    "is_startup_enabled",
    "disable_all",
]

_IS_WINDOWS = sys.platform == "win32"

#: 启动项名字前缀。带前缀是为了能一眼认出哪些是这个程序写的，也能一次全清掉。
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME_PREFIX = "myautowork-checklist-"


class StartupUnavailable(RuntimeError):
    """这台机器上做不了开机启动。"""


def available() -> bool:
    return _IS_WINDOWS


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "myautowork"


def bootstrap_path(checklist: str) -> Path:
    """引导文件放哪。名字按清单名生成，**必须过 safe_slug** —— 清单名是用户随便起的，
    里面可能有斜杠和冒号，直接拼路径等于让用户输入决定写到哪去。"""
    return data_dir() / "startup" / f"{safe_slug(checklist)}.pyw"


BOOTSTRAP_TEMPLATE = '''"""开机自动运行清单「{name}」。

这个文件是 myautowork 生成的，**可以删**：删掉之后对应的开机启动项会失效，
在程序的「我的清单 → 设置」里再关一次就会把注册表项也清掉。

想改行为就直接改这里 —— 它是个普通的 Python 脚本。
"""

import sys

sys.path.insert(0, r"{repo}")

from kernel.run import main

if __name__ == "__main__":
    raise SystemExit(
        main(
            [
                "--checklist",
                r"{name}",
                "--workdir",
                r"{workdir}",
                "--workflows-dir",
                r"{workflows_dir}",
                "--plugins-dir",
                r"{plugins_dir}",
            ]
        )
    )
'''


def launch_command(checklist: str, *, repo: Path, workdir: Path,
                   workflows_dir: Path, plugins_dir: Path) -> tuple[list[str], Path]:
    """写出引导文件，返回 ``(启动项命令行, 引导文件路径)``。

    ``pythonw.exe`` 跟当前解释器同目录 —— 用当前解释器意味着"程序现在能跑，开机就能跑"，
    不用去猜用户装的是哪个 Python。
    """
    if not _IS_WINDOWS:
        raise StartupUnavailable("开机运行目前只在 Windows 上实现")

    target = bootstrap_path(checklist)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        BOOTSTRAP_TEMPLATE.format(
            name=checklist,
            repo=str(repo),
            workdir=str(workdir),
            workflows_dir=str(workflows_dir),
            plugins_dir=str(plugins_dir),
        ),
        encoding="utf-8",
    )

    # pythonw 和 python 同目录，把文件名换掉即可。找不到就退回普通解释器 ——
    # 会闪一个黑框，但至少能跑起来，比直接失败强。
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.is_file() else exe

    return [str(interpreter), str(target)], target


def _run_key():
    import winreg  # noqa: PLC0415 - 只有 Windows 上才有

    return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS)


def set_startup(checklist: str, command: list[str] | None) -> bool:
    """开或关一条清单的开机运行。``command=None`` 表示关掉。

    返回 True 表示写成功了。**关的时候找不到那个启动项也算成功** —— 用户要的结果
    （它不会开机跑了）已经达成。
    """
    if not _IS_WINDOWS:
        raise StartupUnavailable("开机运行目前只在 Windows 上实现")

    import winreg  # noqa: PLC0415

    name = NAME_PREFIX + safe_slug(checklist)
    with _run_key() as key:
        if command is None:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                return True
            return True
        # 命令行两段都加引号：路径里有空格时不加就会被拆成两个参数。
        value = " ".join(f'"{part}"' for part in command)
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    return True


def is_startup_enabled(checklist: str) -> bool:
    """注册表里到底有没有这一项。

    **这是唯一可信的判据。** 配置文件里写着 startup=true、但注册表项被人删了（或者反过来）
    是很常见的 —— 以文件为准会让界面显示"已开启"而实际什么都不会发生。
    """
    if not _IS_WINDOWS:
        return False

    import winreg  # noqa: PLC0415

    name = NAME_PREFIX + safe_slug(checklist)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def disable_all() -> int:
    """把所有 myautowork 写的启动项清掉，返回清了几个。"""
    if not _IS_WINDOWS:
        return 0

    import winreg  # noqa: PLC0415

    removed = 0
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            index = 0
            names: list[str] = []
            while True:
                try:
                    names.append(winreg.EnumValue(key, index)[0])
                except OSError:
                    break
                index += 1
            for name in names:
                if name.startswith(NAME_PREFIX):
                    winreg.DeleteValue(key, name)
                    removed += 1
    except FileNotFoundError:
        return 0
    return removed
