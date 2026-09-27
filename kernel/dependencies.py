"""检查插件声明的依赖装了没有。

**为什么只做"检测"和"给出安装命令"，不自动装。** 自动装依赖意味着内核会在用户没点头的
情况下联网、执行 pip、改动他机器上的环境。在一台离线的生产机器上，那会变成一个卡住不动的
进度条；而在一台装了别的东西的机器上，它可能悄悄升级掉别人依赖的版本。所以：内核负责**说
清楚缺什么**，装不装由用户点那一下。

**为什么要声明而不是"跑起来看报错"。** 真去 `import` 一下最准，但那是**执行插件代码** ——
为了检查依赖而执行它，等于让"看一眼"这个动作有了副作用。宁可保守地用分发包名对一遍。

**为什么不用 importlib.metadata 直接判版本。** 包名和 import 名经常不一样（``Pillow`` ->
``PIL``、``pywin32`` -> ``win32api``），版本比对又需要 ``packaging``。这里只做**装没装**的
判断：**不确定的事情不要假装确定** —— 报"可能缺"比报"一定缺"诚实，也比漏报有用。
"""

from __future__ import annotations

import importlib.metadata as metadata
import re
from dataclasses import dataclass

__all__ = [
    "DependencyStatus",
    "check",
    "distribution_name",
    "from_error",
    "missing",
    "suggest",
]

#: 模块名 -> pip 上的包名。**只收常见的那几个** —— 这张表本质上是猜，不该装作很全。
#: 猜错的代价是 `pip install PIL` 报"找不到这个包"，用户看得见；比什么都不做还是好。
_ALIASES = {
    "pil": "Pillow",
    "win32api": "pywin32",
    "win32con": "pywin32",
    "win32gui": "pywin32",
    "win32process": "pywin32",
    "win32clipboard": "pywin32",
    "win32event": "pywin32",
    "pythoncom": "pywin32",
    "pywintypes": "pywin32",
    "cv2": "opencv-python",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "serial": "pyserial",
    "dateutil": "python-dateutil",
    "attr": "attrs",
}

_MODULE_ERROR = re.compile(r"No module named '([A-Za-z0-9_.]+)'")

#: ``openpyxl>=3.1`` / ``Pillow`` / ``requests[security]==2.31.0`` 里的包名部分。
_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def distribution_name(requirement: str) -> str:
    """从一行 pip 要求里取出分发包名。取不出来就原样返回，交给 pip 报错。"""
    match = _REQUIREMENT.match(requirement or "")
    return match.group(1) if match else (requirement or "").strip()


@dataclass(frozen=True)
class DependencyStatus:
    """一个依赖的检查结果。"""

    requirement: str
    installed: bool
    version: str = ""

    @property
    def name(self) -> str:
        return distribution_name(self.requirement)

    def describe(self) -> str:
        if self.installed:
            return f"{self.requirement}（已装 {self.version}）" if self.version else self.requirement
        return f"{self.requirement}（缺）"


def check(dependencies: tuple[str, ...] | list[str]) -> list[DependencyStatus]:
    """逐个检查。**任何意外都当作"缺"** —— 查不出来不等于装好了。"""
    result: list[DependencyStatus] = []
    for requirement in dependencies:
        name = distribution_name(requirement)
        if not name:
            continue
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            result.append(DependencyStatus(requirement, False))
        except Exception:
            # 元数据坏了、环境畸形…… 一律按"缺"处理，让用户有机会去修。
            result.append(DependencyStatus(requirement, False))
        else:
            result.append(DependencyStatus(requirement, True, version))
    return result


def missing(dependencies: tuple[str, ...] | list[str]) -> list[str]:
    """只返回没装的那些（保持声明顺序，去掉重复）。"""
    seen: set[str] = set()
    result: list[str] = []
    for status in check(dependencies):
        if status.installed:
            continue
        name = status.name.lower()
        if name and name not in seen:
            seen.add(name)
            result.append(status.requirement)
    return result


def from_error(text: str) -> list[str]:
    """从加载失败的报错里认出缺的包名。

    **这是兜底，不是主路径。** 声明是最准的；但没有声明时，"跑起来撞了什么"是唯一能拿到的
    线索。模块名未必等于 pip 上的包名（``PIL`` 其实是 ``Pillow``），常见的做了映射，其余按
    原样交给 pip —— 装不上 pip 会自己报出来，比什么都不做还是好。
    """
    out: list[str] = []
    seen: set[str] = set()
    for found in _MODULE_ERROR.findall(text or ""):
        root = found.split(".")[0]
        key = root.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(_ALIASES.get(key, root))
    return out


def suggest(dependencies: tuple[str, ...] | list[str], error: str = "") -> list[str]:
    """该给用户装什么：**声明的缺项 + 从报错里认出来的**。

    两条都要：声明是插件作者写下的意图，报错是它实际撞上的墙。只看声明的话，一个没写
    ``dependencies`` 的插件会把用户卡死在 ModuleNotFoundError 上，而界面上连个按钮都没有。
    """
    out = list(missing(dependencies))
    seen = {item.lower() for item in out}
    for name in from_error(error):
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out
