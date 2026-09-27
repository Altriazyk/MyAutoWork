"""把文件或目录删到**回收站**。

**为什么不用 ``shutil.rmtree``。** 这里删的是用户自己写的流程和插件 —— 一次误点就永久没了。
走回收站，误删还能捞回来。这不是"多一层保险"，是"删用户东西"这件事本身该有的分寸。

**为什么用 ``SHFileOperationW`` 而不是更现代的 ``IFileOperation``。** 后者要先初始化 COM、
实现 ``IShellItem`` 那一串接口，为了删一个目录不值得。``SHFileOperationW`` 虽然被官方标记为
过时，但一直能用，而且**不需要 COM**。

**非 Windows 上退回永久删除。** 与其假装有回收站（然后悄悄真删掉），不如行为明确 ——
调用方从 ``to_recycle_bin`` 的返回值就知道到底发生了什么。
"""

from __future__ import annotations

import ctypes
import shutil
import sys
from pathlib import Path

__all__ = ["to_recycle_bin", "RecycleUnavailable"]

_IS_WINDOWS = sys.platform == "win32"

#: SHFileOperation 的操作码与标志
_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400


class RecycleUnavailable(RuntimeError):
    """系统不提供回收站（或者这个操作不支持它）。"""


if _IS_WINDOWS:

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]


def to_recycle_bin(path: str | Path) -> bool:
    """把 ``path`` 删到回收站。返回 True 表示走了回收站，False 表示是永久删除。

    路径必须是**绝对路径** —— ``SHFileOperationW`` 对相对路径的行为跟当前工作目录绑定，
    而工作目录在自动化运行时是会变的，这是个很容易埋进去的坑。
    """
    target = Path(path).resolve()
    if not target.exists():
        raise FileNotFoundError(f"要删的东西不存在：{target}")

    if not _IS_WINDOWS:
        _force_remove(target)
        return False

    # pFrom 要以**两个** \0 结尾：一个结束字符串，一个结束列表。
    operation = _SHFILEOPSTRUCTW()
    operation.wFunc = _FO_DELETE
    operation.pFrom = str(target) + "\0\0"
    operation.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0:
        # 常见原因：目录里有进程占着的文件（插件没停干净时就是这种）。
        raise OSError(
            f"删到回收站失败（SHFileOperation 返回 {result}）：{target}\n"
            "如果有程序正占着里面的文件，先停掉它再试。"
        )
    if operation.fAnyOperationsAborted:
        raise OSError(f"删除被中止：{target}")
    return True


def _force_remove(target: Path) -> None:
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=False)
    else:
        target.unlink()
