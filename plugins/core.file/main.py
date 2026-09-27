"""文件操作：读、复制、移动、删除、列目录、看路径信息。

**为什么和「写文件」分成两个插件。** ``core.file.write`` 是第一个插件，当时只做了写入，
id 里就带了 ``write``。现在补齐读取这一半，如果硬塞回去，那个 id 就变成了一个什么都做
的名字。分成两个之后各自的名字都还准确 —— 代价是文件相关的动作分布在两个插件里，
点开插件的动作列表看不出全貌。

**删除是真的删。** 界面上删流程/插件走回收站，那是"删用户自己写的东西"；而这个动作是
流程里的一步，跑起来没人盯着，去回收站反而会让"删了就删了"这个语义变模糊 ——
用户看一眼回收站，分不清哪次是手抖、哪次是流程干的。所以这里是永久删除，
description 里写明。
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from myautowork import (
    Bool,
    Context,
    Enum,
    File,
    Folder,
    Integer,
    String,
    Text,
    action,
)

__all__ = ["read", "copy", "move", "delete", "list_dir", "exists", "path_info", "make_dir"]

#: 读文件时最多回传多少字符。防的是一个手滑选中了几百兆的日志，
#: 结果把整份内容塞进变量、再塞进运行历史 —— 那不是"读到了"，是把程序撑死。
MAX_TEXT = 2_000_000


def _as_path(value: Any) -> Path:
    text = str(value or "").strip().strip('"')
    if not text:
        raise ValueError("没有给路径")
    return Path(text).expanduser()


def _describe(path: Path) -> dict[str, Any]:
    """路径的常见信息。目录和文件都能用。"""
    info: dict[str, Any] = {
        "path": str(path),
        "name": path.name,
        "stem": path.stem,
        "suffix": path.suffix,
        "parent": str(path.parent),
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "exists": path.exists(),
    }
    if path.is_file():
        stat = path.stat()
        info["size"] = stat.st_size
        info["modified"] = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
        info["created"] = datetime.fromtimestamp(stat.st_ctime).isoformat(timespec="seconds")
    return info


@action(
    id="read",
    name="读文件",
    category="文件",
    icon="file-text",
    description="把文件内容读成文本或行列表。读不到就走出错出口",
    inputs={
        "path": File(required=True, label="文件"),
        "mode": Enum(["整个文件", "按行拆分", "前 N 行"], default="整个文件", label="怎么读"),
        "lines": Integer(default=20, label="读几行", help="「前 N 行」时用"),
        "encoding": String(default="utf-8", label="编码"),
        "strip": Bool(default=False, label="每行去首尾空白"),
    },
    outputs={
        "text": String(label="整份文本"),
        "lines": Text(label="按行"),
        "count": Integer(label="行数"),
        "bytes": Integer(label="字节数"),
    },
)
def read(
    ctx: Context,
    path: Any,
    mode: str = "整个文件",
    lines: int = 20,
    encoding: str = "utf-8",
    strip: bool = False,
    **_: Any,
) -> dict[str, Any]:
    target = _as_path(path)
    if not target.is_file():
        # 走 error 出口而不是给个空字符串 —— "文件不在"和"文件是空的"是两件事，
        # 悄悄返回空串会让下游拿着空数据继续跑，最后在一个不相干的地方报错。
        raise FileNotFoundError(f"找不到文件：{target}")

    raw = target.read_bytes()
    try:
        content = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{target.name} 用 {encoding} 解不开（{exc.reason}，第 {exc.start} 字节）。"
            "它可能是二进制文件，或者真实编码不是这个 —— 试试 gbk / utf-8-sig"
        ) from exc

    # **换行一律规整成 \n。** 不规整的话，同一个 text 输出在不同 mode 下含义不一样：
    # 「整个文件」给原文（CRLF），「前 N 行」是拼出来的（LF）—— 下游拿去做匹配时
    # 一个能对上、一个对不上，而那种错极难看出来（两串在编辑器里长得一模一样）。
    # 真正的字节数由 bytes 输出给，那里不受影响。
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    rows = content.splitlines()
    if strip:
        rows = [row.strip() for row in rows]

    if mode == "按行拆分":
        text = content
    elif mode == "前 N 行":
        rows = rows[: max(0, int(lines))]
        text = "\n".join(rows)
    else:
        text = content

    if len(text) > MAX_TEXT:
        raise ValueError(
            f"{target.name} 有 {len(text)} 个字符，超过上限 {MAX_TEXT}。"
            "用「前 N 行」，或者先在外面切一份出来"
        )

    ctx.info(f"读了 {target.name}：{len(rows)} 行，{len(raw)} 字节")
    return {
        "text": text,
        "lines": "\n".join(rows),
        "count": len(rows),
        "bytes": len(raw),
    }


@action(
    id="copy",
    name="复制文件",
    category="文件",
    icon="copy",
    description="把文件或整个目录复制到别处。目标目录会自动建出来",
    inputs={
        "source": File(required=True, label="来源"),
        "target": String(required=True, label="复制到", help="可以是目录，也可以是新文件名"),
        "overwrite": Bool(default=False, label="覆盖已有的"),
    },
    outputs={"path": String(label="落地路径"), "bytes": Integer(label="字节数")},
)
def copy(
    ctx: Context, source: Any, target: Any, overwrite: bool = False, **_: Any
) -> dict[str, Any]:
    src = _as_path(source)
    if not src.exists():
        raise FileNotFoundError(f"找不到来源：{src}")

    dst = _as_path(target)
    # 给的是个目录（或者以分隔符结尾），就按"复制进去"理解 —— 这是复制粘贴的直觉。
    if dst.is_dir():
        dst = dst / src.name

    if dst.exists():
        if not overwrite:
            raise FileExistsError(
                f"{dst} 已经存在。要替换它就把「覆盖已有的」打开"
            )
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()

    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
        size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    else:
        shutil.copy2(src, dst)
        size = dst.stat().st_size

    ctx.info(f"复制 {src.name} -> {dst}")
    return {"path": str(dst), "bytes": size}


@action(
    id="move",
    name="移动 / 重命名",
    category="文件",
    icon="move",
    description="把文件或目录挪到别处。同一目录内换名字就是重命名",
    inputs={
        "source": File(required=True, label="来源"),
        "target": String(required=True, label="挪到", help="可以是目录，也可以是新名字"),
        "overwrite": Bool(default=False, label="覆盖已有的"),
    },
    outputs={"path": String(label="落地路径")},
)
def move(
    ctx: Context, source: Any, target: Any, overwrite: bool = False, **_: Any
) -> dict[str, Any]:
    src = _as_path(source)
    if not src.exists():
        raise FileNotFoundError(f"找不到来源：{src}")

    dst = _as_path(target)
    if dst.is_dir():
        dst = dst / src.name

    if dst.exists():
        if not overwrite:
            raise FileExistsError(f"{dst} 已经存在。要替换它就把「覆盖已有的」打开")
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    ctx.info(f"移动 {src.name} -> {dst}")
    return {"path": str(dst)}


@action(
    id="delete",
    name="删除文件",
    category="文件",
    icon="trash",
    description="**永久删除**，不进回收站。删目录时整个目录一起没",
    inputs={
        "path": File(required=True, label="要删的"),
        "missing_ok": Bool(default=False, label="不存在就当成功", help="用在「删不删都行」的场景"),
    },
    outputs={"deleted": Bool(label="真的删了"), "path": String(label="路径")},
)
def delete(ctx: Context, path: Any, missing_ok: bool = False, **_: Any) -> dict[str, Any]:
    target = _as_path(path)
    if not target.exists():
        if missing_ok:
            ctx.info(f"{target} 本来就不在，跳过")
            return {"deleted": False, "path": str(target)}
        raise FileNotFoundError(f"找不到：{target}")

    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    ctx.warning(f"已永久删除 {target}")
    return {"deleted": True, "path": str(target)}


@action(
    id="list_dir",
    name="列目录",
    category="文件",
    icon="folder",
    description="列出目录里的文件。可以按通配符筛、可以递归",
    inputs={
        "folder": Folder(required=True, label="目录"),
        "pattern": String(default="*", label="通配符", help="例如 *.txt、报表*"),
        "recursive": Bool(default=False, label="包含子目录"),
        "files_only": Bool(default=True, label="只要文件", help="关掉的话目录也会列出来"),
    },
    outputs={
        "files": Text(label="每个一行"),
        "count": Integer(label="个数"),
        "first": String(label="第一个"),
    },
)
def list_dir(
    ctx: Context,
    folder: Any,
    pattern: str = "*",
    recursive: bool = False,
    files_only: bool = True,
    **_: Any,
) -> dict[str, Any]:
    root = _as_path(folder)
    if not root.is_dir():
        raise NotADirectoryError(f"不是目录（或者不存在）：{root}")

    found = root.rglob(pattern) if recursive else root.glob(pattern)
    rows = [
        p for p in found
        if (p.is_file() if files_only else True)
    ]
    rows.sort(key=lambda p: str(p).lower())

    text = "\n".join(str(p) for p in rows)
    ctx.info(f"{root.name} 里找到 {len(rows)} 项" + (f"，通配符 {pattern}" if pattern != "*" else ""))
    return {
        "files": text,
        "count": len(rows),
        "first": str(rows[0]) if rows else "",
    }


@action(
    id="exists",
    name="存在吗",
    category="文件",
    icon="help-circle",
    description="判断文件或目录在不在。**不在不算失败** —— 结果走布尔值",
    inputs={
        "path": String(required=True, label="路径"),
        "kind": Enum(["什么都行", "必须是文件", "必须是目录"], default="什么都行", label="要求"),
    },
    outputs={"exists": Bool(label="在吗"), "path": String(label="路径")},
)
def exists(ctx: Context, path: Any, kind: str = "什么都行", **_: Any) -> dict[str, Any]:
    target = _as_path(path)
    if kind == "必须是文件":
        ok = target.is_file()
    elif kind == "必须是目录":
        ok = target.is_dir()
    else:
        ok = target.exists()
    ctx.info(f"{target} {'在' if ok else '不在'}")
    return {"exists": ok, "path": str(target)}


@action(
    id="path_info",
    name="路径信息",
    category="文件",
    icon="info",
    description="拆一个路径：所在目录、文件名、后缀、大小、修改时间",
    inputs={"path": String(required=True, label="路径")},
    outputs={
        "name": String(label="文件名"),
        "stem": String(label="不含后缀的名字"),
        "suffix": String(label="后缀"),
        "parent": String(label="所在目录"),
        "size": Integer(label="字节数"),
        "modified": String(label="修改时间"),
        "is_file": Bool(label="是文件吗"),
        "exists": Bool(label="在吗"),
    },
)
def path_info(ctx: Context, path: Any, **_: Any) -> dict[str, Any]:
    info = _describe(_as_path(path))
    ctx.info(f"{info['path']}：{'文件' if info['is_file'] else '目录' if info['is_dir'] else '不存在'}")
    return info


@action(
    id="make_dir",
    name="建目录",
    category="文件",
    icon="folder-plus",
    description="把目录建出来（中间缺的层级一起建）。已经在了就算了",
    inputs={"folder": String(required=True, label="目录")},
    outputs={"path": String(label="路径"), "created": Bool(label="新建的吗")},
)
def make_dir(ctx: Context, folder: Any, **_: Any) -> dict[str, Any]:
    target = _as_path(folder)
    created = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    ctx.info(f"{'建好' if created else '已经有了'} {target}")
    return {"path": str(target), "created": created}
