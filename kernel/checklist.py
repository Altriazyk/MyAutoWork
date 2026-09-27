"""清单的存储格式。

**为什么放在内核而不是界面里。** 命令行（``myautowork --checklist 名字``）和图形界面读的是
**同一个文件**。各自写一份解析代码，迟早会漂移 —— 一边认得的格式另一边不认，而用户看到的
是"开机运行没反应"这种没有线索的现象。所以格式只有这一份。

文件形状::

    {
      "current": "默认清单",
      "checklists": {"默认清单": ["a.json", "b.json"]},
      "settings": {"默认清单": {"startup": true}}
    }

``settings`` 是后加的。老文件没有这个键也能读（少了就当成默认值），
**新键永远是往后加的** —— 升级不该让用户之前勾过的东西丢掉。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "CHECKLIST_FILE",
    "CHECKLIST_DIR",
    "DEFAULT_CHECKLIST",
    "ChecklistSettings",
    "ChecklistFile",
    "default_path",
    "legacy_paths",
    "resolve_path",
    "load",
    "save",
    "load_checklists",
    "save_checklists",
    "ordered_workflows",
    "safe_slug",
]

#: 清单存在哪（相对工作目录）。
CHECKLIST_FILE = "checklists.json"

#: 清单数据的目录名。**和 ``workflows/`` 平级** —— 两个都是"这个工作区里的东西"，
#: 摆在同一层，用户一眼能看出它们是同一类；也不会因为换了工作目录就跟着跑掉。
CHECKLIST_DIR = "checklists"

#: 早先短暂用过的目录名，只为了让迁移认得它。
_LEGACY_CONFIG_DIR = "config"

#: 一条清单都没有时的初始名字。
DEFAULT_CHECKLIST = "默认清单"


@dataclass
class ChecklistSettings:
    """一条清单自己的设置。"""

    #: 开机后自动跑这条清单。
    startup: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {"startup": bool(self.startup)}

    @classmethod
    def from_dict(cls, data: object) -> "ChecklistSettings":
        if not isinstance(data, dict):
            return cls()
        return cls(startup=bool(data.get("startup", False)))


@dataclass
class ChecklistFile:
    """整个清单文件。"""

    current: str = ""
    checklists: dict[str, list[str]] = field(default_factory=dict)
    settings: dict[str, ChecklistSettings] = field(default_factory=dict)

    def settings_for(self, name: str) -> ChecklistSettings:
        """取某条清单的设置。**没建过就现给一份默认的**，不要顺手写进去 ——
        查询不该有副作用，否则读一次文件就把它改了。"""
        return self.settings.get(name, ChecklistSettings())

    def startup_names(self) -> list[str]:
        return sorted(n for n, s in self.settings.items() if s.startup and n in self.checklists)


def default_path(workflows_dir: str | Path) -> Path:
    """清单文件该放哪：**``<workflows 的同级>/checklists/checklists.json``**。

    判据是 **workflows 目录的位置**，不是"当前工作目录" —— 后者是启动时所在的地方，
    换个目录启动就会指向另一个文件。清单属于这个工作区，就该锚在工作区上。
    """
    return Path(workflows_dir).resolve().parent / CHECKLIST_DIR / CHECKLIST_FILE


def legacy_paths(workdir: str | Path) -> list[Path]:
    """历史上放过清单的位置，**新的排在前面**。

    两个都要认：最早直接摊在工作目录根上，中间短暂地放去过 ``config/``。
    用户可能在任何一步停下来，迁移得能接上。
    """
    root = Path(workdir)
    return [root / _LEGACY_CONFIG_DIR / CHECKLIST_FILE, root / CHECKLIST_FILE]


def resolve_path(
    workflows_dir: str | Path, workdir: str | Path | None = None, *, migrate: bool = True
) -> Path:
    """挑实际该读写哪个路径，并把老位置的文件**搬过来**。

    只在新位置没有、老位置有的时候搬。搬不动（只读、被占用）就**退回老位置继续用** ——
    为了一个"目录更整齐"的改动而让用户打不开清单，不值得。
    """
    new = default_path(workflows_dir)
    if new.is_file() or not migrate:
        return new

    for old in legacy_paths(workdir or Path.cwd()):
        if not old.is_file() or old.resolve() == new.resolve():
            continue
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            old.replace(new)
        except OSError:
            return old
        return new
    return new


def load(path: str | Path) -> ChecklistFile:
    """读回清单文件。

    **坏文件、缺文件都返回空，不抛异常。** 一份写坏的清单不该让软件打不开 —— 大不了当作
    还没建过清单。旧格式（只有一个清单、直接存 ``{"workflows": [...]}``）做一次迁移：
    那个版本只存在过一轮，但用户已经勾过的状态不该因为升级而丢掉。
    """
    target = Path(path)
    if not target.is_file():
        return ChecklistFile()

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return ChecklistFile()
    if not isinstance(data, dict):
        return ChecklistFile()

    raw_settings = data.get("settings")
    settings: dict[str, ChecklistSettings] = {}
    if isinstance(raw_settings, dict):
        settings = {
            str(name): ChecklistSettings.from_dict(value)
            for name, value in raw_settings.items()
            if isinstance(name, str)
        }

    if "checklists" in data:
        raw = data.get("checklists") or {}
        checklists = {
            str(name): [str(item) for item in (items or []) if isinstance(item, str)]
            for name, items in (raw.items() if isinstance(raw, dict) else [])
            if isinstance(name, str)
        }
        current = str(data.get("current") or "")
        if current not in checklists:
            current = ""
        return ChecklistFile(current=current, checklists=checklists, settings=settings)

    legacy = data.get("workflows")
    if isinstance(legacy, list):
        return ChecklistFile(
            current=DEFAULT_CHECKLIST,
            checklists={
                DEFAULT_CHECKLIST: [str(i) for i in legacy if isinstance(i, str)]
            },
            settings={DEFAULT_CHECKLIST: settings.get(DEFAULT_CHECKLIST, ChecklistSettings())},
        )
    return ChecklistFile(settings=settings)


def save(path: str | Path, data: ChecklistFile) -> None:
    """写回。存不下就静默放弃 —— 丢一次勾选状态，比弹一个错误框打扰用户好。

    **父目录要自己建。** 清单现在住在 ``checklists/`` 子目录里，那个目录不一定存在
    （尤其是没走过迁移、直接新建的时候）。少了这一句，写入会失败并被下面吞掉，
    表现是"保存完再打开，清单没了" —— 而且一句提示都没有。踩过。
    """
    target = Path(path)
    payload = {
        "current": data.current,
        "checklists": data.checklists,
        "settings": {name: item.to_dict() for name, item in data.settings.items()},
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


# -- 兼容接口 ----------------------------------------------------------------
# 界面那边一直用的是这两个函数。留着它们，是为了不让"格式搬家"顺手改动一堆调用点 ——
# 改动面越小，出错的地方越少。


def load_checklists(path: str | Path) -> tuple[dict[str, list[str]], str]:
    data = load(path)
    return data.checklists, data.current


def save_checklists(
    path: str | Path,
    checklists: dict[str, list[str]],
    current: str,
    settings: dict[str, ChecklistSettings] | None = None,
) -> None:
    """写回清单。

    ``settings=None`` 时**保留文件里原有的设置** —— 光改个清单内容不该把开机运行关掉。
    这是最容易出错的地方：读的时候没读 settings、写的时候一把覆盖，用户的设置就没了。
    """
    existing = load(path)
    data = ChecklistFile(
        current=current,
        checklists=checklists,
        settings=settings if settings is not None else existing.settings,
    )
    # 删掉的清单，设置也别留着 —— 否则同名再建一条会继承上一次的开机运行。
    data.settings = {n: s for n, s in data.settings.items() if n in checklists}
    save(path, data)


def ordered_workflows(data: ChecklistFile, name: str) -> list[str]:
    """某条清单里的流程文件名，按顺序。取不到就返回空。"""
    return list(data.checklists.get(name, []))


def safe_slug(name: str) -> str:
    """把清单名变成能当文件名用的东西。

    开机启动项要按清单名生成一个引导文件，而清单名是用户随便起的 —— 里面可能有斜杠、
    冒号、空格。**不能直接拿它拼路径**，那等于让用户输入决定写到哪去。
    """
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (name or "").strip())
    return cleaned[:40] or "checklist"
