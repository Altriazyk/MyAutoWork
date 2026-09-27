"""插件清单（manifest.json）的加载与校验。

为什么要有这个文件、而不是全靠 Python 装饰器声明？

因为**界面需要在启动任何插件进程之前就知道有哪些插件**。左侧模块面板要在用户
拖第一个节点之前就画出来，如果那时候得把几十个插件进程全拉起来，启动就是灾难。

所以职责切成两半：

- ``manifest.json``  身份 + 入口 + 能力索引（静态、可秒读）
- 装饰器              完整的输入输出 schema（需要执行代码才能拿到）

两者会被交叉校验：manifest 里 ``provides`` 声明了但代码里没有的动作，会报错。
这样第三方插件不会出现"装上了但面板里点不动"的情况。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Mapping

from .errors import ManifestError

__all__ = ["API_VERSION", "Runtime", "Manifest", "load_manifest", "MANIFEST_FILENAME"]

#: 内核支持的插件契约版本。插件的 api_version 必须与它相同。
API_VERSION = "1"

MANIFEST_FILENAME = "manifest.json"

_REQUIRED_KEYS = ("id", "name")
_VALID_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


@dataclass(frozen=True)
class Runtime:
    """插件怎么跑起来。"""

    type: str = "python"
    entry: str = "main.py"
    #: 指定解释器路径。留空则用内核自己的解释器。
    python: str | None = None
    #: 相对于插件目录的虚拟环境目录名。存在则优先用它（解决依赖冲突）。
    venv: str | None = ".venv"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Runtime":
        if not data:
            return cls()
        unknown = set(data) - {"type", "entry", "python", "venv"}
        if unknown:
            raise ManifestError(f"runtime 里有无法识别的字段：{sorted(unknown)}")
        rtype = str(data.get("type", "python"))
        if rtype != "python":
            raise ManifestError(f"暂不支持的 runtime.type：{rtype!r}（当前只支持 'python'）")
        return cls(
            type=rtype,
            entry=str(data.get("entry", "main.py")),
            python=data.get("python"),
            venv=data.get("venv", ".venv"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "entry": self.entry, "python": self.python, "venv": self.venv}


@dataclass
class Manifest:
    """一个插件的静态描述。"""

    id: str
    name: str
    path: Path
    version: str = "0.1.0"
    api_version: str = API_VERSION
    description: str = ""
    author: str = ""
    homepage: str = ""
    category: str = "通用"
    keywords: tuple[str, ...] = ()
    runtime: Runtime = dc_field(default_factory=Runtime)
    #: 需要的第三方包，pip 要求的写法：``["openpyxl>=3.1", "Pillow"]``。
    #: 只是**声明**——内核不会自动装（离线机器上自动装会直接失败）。装了没有由界面查、
    #: 由用户点一下再装。缺了这一层，用户会先拖到画布上跑，才撞 ModuleNotFoundError。
    dependencies: tuple[str, ...] = ()
    #: 静态能力索引，形如 {"actions": ["write"], "triggers": ["start"]}
    provides: dict[str, list[str]] = dc_field(default_factory=dict)
    raw: dict[str, Any] = dc_field(default_factory=dict)

    # -- 路径 -----------------------------------------------------------------

    @property
    def entry_path(self) -> Path:
        return self.path / self.runtime.entry

    def python_executable(self, fallback: str) -> str:
        """挑出启动这个插件该用的解释器。

        优先级：插件自带的 venv > manifest 里指定的路径 > 内核自己的解释器。

        自带 venv 是 Python 插件生态的必需品 —— 两个插件分别依赖 requests 2.1 和
        2.31 是迟早会发生的事，靠独立 venv 隔开比靠约束版本现实得多。
        """
        if self.runtime.venv:
            venv_dir = self.path / self.runtime.venv
            candidates = (
                venv_dir / "Scripts" / "python.exe",  # Windows
                venv_dir / "bin" / "python",  # POSIX
            )
            for candidate in candidates:
                if candidate.exists():
                    return str(candidate)
        if self.runtime.python:
            return self.runtime.python
        return fallback

    @property
    def has_own_venv(self) -> bool:
        if not self.runtime.venv:
            return False
        venv_dir = self.path / self.runtime.venv
        return (venv_dir / "Scripts" / "python.exe").exists() or (
            venv_dir / "bin" / "python"
        ).exists()

    # -- 序列化 ---------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, path: Path) -> "Manifest":
        for key in _REQUIRED_KEYS:
            if not data.get(key):
                raise ManifestError(f"{path} 缺少必填字段 {key!r}")

        pid = str(data["id"])
        bad = set(pid) - _VALID_ID_CHARS
        if bad:
            raise ManifestError(f"插件 id 含有非法字符 {sorted(bad)}：{pid!r}")
        if pid != path.name:
            raise ManifestError(
                f"插件 id 与目录名不一致：id={pid!r}，目录={path.name!r}。"
                "请保持一致，否则插件查找会变得难以预测。"
            )

        api_version = str(data.get("api_version", API_VERSION))
        if api_version != API_VERSION:
            raise ManifestError(
                f"插件 {pid} 声明 api_version={api_version}，内核支持 {API_VERSION}。"
                "契约版本不匹配时拒绝加载，好过运行时才出问题。"
            )

        provides_raw = data.get("provides") or {}
        if not isinstance(provides_raw, Mapping):
            raise ManifestError(f"插件 {pid} 的 provides 必须是对象")
        provides: dict[str, list[str]] = {}
        for key in ("actions", "triggers"):
            value = provides_raw.get(key) or []
            if isinstance(value, str):
                value = [value]
            provides[key] = [str(v) for v in value]

        keywords = data.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]

        dependencies = data.get("dependencies") or []
        if isinstance(dependencies, str):
            dependencies = [dependencies]

        return cls(
            id=pid,
            name=str(data["name"]),
            path=path,
            version=str(data.get("version", "0.1.0")),
            api_version=api_version,
            description=str(data.get("description", "")),
            author=str(data.get("author", "")),
            homepage=str(data.get("homepage", "")),
            category=str(data.get("category", "通用")),
            keywords=tuple(str(k) for k in keywords),
            runtime=Runtime.from_dict(data.get("runtime")),
            dependencies=tuple(str(d).strip() for d in dependencies if str(d).strip()),
            provides=provides,
            raw=dict(data),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "api_version": self.api_version,
            "description": self.description,
            "author": self.author,
            "homepage": self.homepage,
            "category": self.category,
            "keywords": list(self.keywords),
            "runtime": self.runtime.to_dict(),
            "provides": self.provides,
        }

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"<Manifest {self.id} {self.version}>"


def load_manifest(plugin_dir: str | Path) -> Manifest:
    """读取并校验一个插件目录下的 manifest.json。"""
    path = Path(plugin_dir).resolve()
    if not path.is_dir():
        raise ManifestError(f"插件目录不存在：{path}")

    manifest_path = path / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ManifestError(f"缺少 {MANIFEST_FILENAME}：{manifest_path}")

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{manifest_path} 不是合法 JSON：{exc}") from exc
    except OSError as exc:
        raise ManifestError(f"无法读取 {manifest_path}：{exc}") from exc

    if not isinstance(data, Mapping):
        raise ManifestError(f"{manifest_path} 的顶层必须是对象")

    manifest = Manifest.from_dict(data, path=path)

    if not manifest.entry_path.is_file():
        raise ManifestError(
            f"插件 {manifest.id} 的入口文件不存在：{manifest.entry_path}"
            f"（runtime.entry = {manifest.runtime.entry!r}）"
        )
    return manifest
