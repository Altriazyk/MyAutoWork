"""把插件压缩包解压到插件目录。

**这个文件是整个项目里最需要小心的地方** —— 它要处理的是**外部来的、不可信的压缩包**。
一个恶意（或者只是做得不严谨）的 zip 可以让解压写到磁盘上任何地方：

- 成员名写成 ``../../../../Windows/System32/x.dll`` —— 路径穿越
- 成员名是绝对路径或者带盘符 ``C:\\x`` —— 直接指定目标
- 成员是个**符号链接**，指向别处，之后往里写东西 —— 顺着链接跑到目录外面
- 几万个文件 / 解压后几个 GB —— 撑爆磁盘（zip bomb）

所以这里**逐个成员校验**，任何一条不满足就整包拒绝，而不是"跳过那一个、继续装剩下的"。
半装进去的插件比装不上更难查：用户以为装好了，实际少了一半文件。

**先看后装。** ``inspect()`` 只读不写，把包里的情况（插件 id、名字、版本、文件数、
会不会覆盖已有插件）拿出来给界面确认；``install()`` 才动磁盘。分开是为了让"确认"这一步
有真东西可确认，而不是先装完再问。
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import MyAutoWorkError
from .manifest import MANIFEST_FILENAME, load_manifest

__all__ = ["PackageInfo", "PluginPackageError", "inspect", "install"]

#: 一个插件包最多允许多少个文件。正常的插件是几十个，上万个只可能是别的东西。
MAX_FILES = 20000

#: 解压后最大多少字节。挡住明显的 zip bomb。
MAX_BYTES = 512 * 1024 * 1024


class PluginPackageError(MyAutoWorkError):
    """这个包不能装。消息直接给用户看。"""


@dataclass
class PackageInfo:
    """一个插件包的内容预览。**只是读出来的，磁盘还没动。**"""

    zip_path: Path
    #: 插件在压缩包里的目录前缀，空字符串表示压缩包的根就是插件目录
    root: str
    plugin_id: str
    name: str
    version: str
    file_count: int
    total_bytes: int
    #: 目标目录已经存在（装下去就是覆盖）
    conflict: bool
    target: Path

    def describe(self) -> str:
        where = f"{self.root}/" if self.root else "压缩包根目录"
        lines = [
            f"插件：{self.name}（{self.plugin_id}）",
            f"版本：{self.version}",
            f"位置：{where}",
            f"内容：{self.file_count} 个文件，解压后约 {self.total_bytes / 1024:.0f} KB",
            f"装到：{self.target}",
        ]
        if self.conflict:
            lines.append("⚠ 目标目录已存在 —— 继续会**覆盖**它（原有文件会被替换）")
        return "\n".join(lines)


def _member_name(info: zipfile.ZipInfo) -> str:
    """把成员名规整成 POSIX 风格，方便逐段检查。"""
    return info.filename.replace("\\", "/")


def _reject_reason(info: zipfile.ZipInfo) -> str:
    """这个成员能不能安全地解压。返回空字符串表示可以。"""
    raw = _member_name(info)
    name = raw.strip()

    if not name:
        return "成员名是空的"

    # 绝对路径：POSIX 的 /，或者 Windows 的 C:\ 和 UNC 的 \\server
    if name.startswith("/") or name.startswith("//"):
        return "是绝对路径"
    if len(name) >= 2 and name[1] == ":":
        return "带盘符"

    parts = PurePosixPath(name).parts
    if ".." in parts:
        return "试图往上级目录写（..）"

    # 符号链接：解压出来是个链接，后续写入会顺着它跑到目录外面。
    # 权限位在 external_attr 的高 16 位，用 S_IFLNK 判断。
    mode = info.external_attr >> 16
    if mode and (mode & 0o170000) == 0o120000:
        return "是符号链接"

    return ""


def _collect(archive: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], int, str]:
    """校验所有成员，返回 ``(可用的成员, 解压后总字节, 拒绝原因)``。

    **一个成员不合格就整包拒绝。** 跳过它继续装的话，用户会拿到一个缺文件的插件 ——
    那比装不上难查得多，因为界面上它看起来是装好了的。
    """
    files: list[zipfile.ZipInfo] = []
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        reason = _reject_reason(info)
        if reason:
            raise PluginPackageError(
                f"这个压缩包不安全，已拒绝：成员 {info.filename!r} {reason}。\n"
                "插件包只应该包含插件自己的文件，正常的包不会出现这种情况。"
            )
        files.append(info)
        total += info.file_size

    if not files:
        raise PluginPackageError("这个压缩包里一个文件都没有")
    if len(files) > MAX_FILES:
        raise PluginPackageError(
            f"压缩包里有 {len(files)} 个文件，超过上限 {MAX_FILES} —— 插件不该这么大"
        )
    if total > MAX_BYTES:
        raise PluginPackageError(
            f"解压后约 {total / 1024 / 1024:.0f} MB，超过上限 {MAX_BYTES // 1024 // 1024} MB"
        )
    return files, total, ""


def _find_root(archive: zipfile.ZipFile, files: list[zipfile.ZipInfo]) -> str:
    """找出插件目录在压缩包里的哪一层。

    两种常见打包方式都要认：直接把插件目录压进去（根下只有它一个文件夹），
    或者进到插件目录里再全选压缩（根下就是 manifest.json）。
    """
    names = {_member_name(info) for info in files}
    if MANIFEST_FILENAME in names:
        return ""

    # 根下只有一个顶层目录，而且它的里面（任意深度）有 manifest.json
    tops = {PurePosixPath(name).parts[0] for name in names if len(PurePosixPath(name).parts) > 1}
    if len(tops) == 1:
        top = tops.pop()
        if f"{top}/{MANIFEST_FILENAME}" in names:
            return top

    found = "、".join(sorted(names)[:8]) or "（空）"
    raise PluginPackageError(
        "压缩包里找不到 manifest.json，这不像是一个插件包。\n"
        "插件包的形状应该是：压缩包根下（或者唯一的一层文件夹里）有 manifest.json 和 main.py。\n"
        f"包里实际有：{found}"
    )


def inspect(zip_path: str | Path) -> PackageInfo:
    """读一个插件包看看里面是什么。**不写磁盘。**"""
    path = Path(zip_path)
    if not path.is_file():
        raise PluginPackageError(f"找不到这个文件：{path}")
    if not zipfile.is_zipfile(path):
        raise PluginPackageError(
            f"{path.name} 不是 zip 压缩包。\n"
            "把插件目录打成 .zip 再导入（右键 → 发送到 → 压缩(zipped)文件夹）。"
        )

    try:
        with zipfile.ZipFile(path) as archive:
            files, total, _ = _collect(archive)
            root = _find_root(archive, files)
            prefix = f"{root}/" if root else ""
            manifest_names = [
                _member_name(i) for i in files if _member_name(i) == f"{prefix}{MANIFEST_FILENAME}"
            ]
            if not manifest_names:
                raise PluginPackageError("找到了目录却读不到 manifest.json")
            raw = archive.read(manifest_names[0])
    except zipfile.BadZipFile as exc:
        raise PluginPackageError(f"压缩包读不了，可能下载不完整：{exc}") from exc

    # **这里只做轻量解析，不调用 load_manifest。** 那个函数是对**完整目录**做校验的：
    # 它要求 id 和目录名一致、还要求 runtime.entry 那个文件真的存在。拿它验一个孤立的
    # manifest 是验不过的 —— 那两关本来就不该在这一步过。
    #
    # 完整校验放在 install() 里、解压到暂存目录之后做。那时文件是全的，校验才有意义；
    # 而且失败的话暂存目录一删，插件目录里什么都不会留下。
    import json  # noqa: PLC0415
    import re  # noqa: PLC0415

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise PluginPackageError(f"manifest.json 不是合法的 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise PluginPackageError("manifest.json 的顶层必须是一个对象")

    # id 要**先于内核校验**被用作目录名，所以判据自己保守：只允许能安全当目录名的字符。
    # 拿一个没验过的字符串去拼路径，正是这个文件要防的那类事。
    raw_id = str(data.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", raw_id):
        raise PluginPackageError(f"manifest.json 里的 id 不合法：{raw_id!r}")

    return PackageInfo(
        zip_path=path,
        root=root,
        plugin_id=raw_id,
        name=str(data.get("name") or raw_id),
        version=str(data.get("version") or "?"),
        file_count=len(files),
        total_bytes=total,
        conflict=False,  # 由 plan() 按 plugins_dir 填
        target=path,  # 同上
    )


def plan(info: PackageInfo, plugins_dir: str | Path) -> PackageInfo:
    """把"装到哪、会不会覆盖"补进预览里。"""
    target = Path(plugins_dir) / info.plugin_id
    return PackageInfo(
        zip_path=info.zip_path,
        root=info.root,
        plugin_id=info.plugin_id,
        name=info.name,
        version=info.version,
        file_count=info.file_count,
        total_bytes=info.total_bytes,
        conflict=target.exists(),
        target=target,
    )


def install(info: PackageInfo, plugins_dir: str | Path, *, overwrite: bool = False) -> Path:
    """按预览把包装进去，返回插件目录。

    **先解压到临时目录，全部成功之后再挪到位。** 直接往目标目录解压的话，中途失败会留下
    一个半成品 —— 而它看起来跟装好的没区别，用户不会知道少了什么。临时目录在同一盘上，
    挪过去是改名，很快。
    """
    target = Path(plugins_dir) / info.plugin_id
    if target.exists() and not overwrite:
        raise PluginPackageError(f"{target} 已经存在。要替换它的话得明确选覆盖。")

    prefix = f"{info.root}/" if info.root else ""
    parent = Path(plugins_dir)
    parent.mkdir(parents=True, exist_ok=True)

    # 暂存目录放在 ``plugins/.staging/<id>``：**用 id 当目录名**，因为 load_manifest 要求
    # id 和目录名一致；放在 plugins 里面是为了**和最终位置同盘** —— 挪过去是改名，很快，
    # 也不会出现"复制到一半失败、留下半个插件"。
    #
    # `.staging` 这一层不会被注册表当成插件：它下面没有 manifest.json（那个在它孙子层）。
    staging_root = parent / ".staging"
    staging = staging_root / info.plugin_id
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    try:
        with zipfile.ZipFile(info.zip_path) as archive:
            # 再校验一遍：inspect 和 install 之间文件可能被换掉（时间差攻击）。
            # 解压路径的检查不能只做一次就当作永远成立。
            files, _total, _ = _collect(archive)
            for member in files:
                name = _member_name(member)
                relative = name[len(prefix) :] if prefix else name
                if not relative:
                    continue
                destination = staging / PurePosixPath(relative)
                # 最后一道：算出来的路径必须真的在暂存目录里面。
                if staging.resolve() not in destination.resolve().parents:
                    raise PluginPackageError(f"成员 {member.filename!r} 会写到插件目录外面，已拒绝")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, open(destination, "wb") as sink:
                    shutil.copyfileobj(source, sink)
    except Exception:
        _discard(staging, staging_root)
        raise

    # 文件齐了，**这时候才做完整校验** —— 这时 id 和目录名天然一致（暂存目录就是用 id
    # 命名的），runtime.entry 也真的在。校验不过就整包丢掉，插件目录里什么都不留。
    try:
        load_manifest(staging)
    except MyAutoWorkError as exc:
        _discard(staging, staging_root)
        raise PluginPackageError(f"这个包装不了：{exc}") from exc

    if target.exists():
        shutil.rmtree(target)
    staging.replace(target)
    # 暂存根空了就收掉，别在插件目录里留一个空文件夹让用户猜。
    try:
        staging_root.rmdir()
    except OSError:
        pass
    return target


def _discard(staging: Path, staging_root: Path) -> None:
    """把装了一半的东西清掉。留着比装不上更难查 —— 它看起来是装好的。"""
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging_root.rmdir()
    except OSError:
        pass
