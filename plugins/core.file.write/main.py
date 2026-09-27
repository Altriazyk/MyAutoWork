"""文件写入插件。

它同时是把类型系统跑通的样本：``path`` 声明成 ``File`` 类型，所以在界面上会自动
带出一个"浏览"按钮；``content`` 声明成 ``Text``（多行），会渲染成多行文本框而不是
单行输入框。插件作者不用写任何界面代码。

相对路径一律走 ``ctx.resolve()`` 解析到运行工作目录。这一点很重要：如果各插件自己
猜 cwd，"文件写到哪去了"会变成永久性的谜题。
"""

from __future__ import annotations

from myautowork import Bool, Context, Enum, File, Integer, String, Text, action


@action(
    id="write",
    name="写文件",
    category="文件",
    icon="file-text",
    description="把文本写入文件，支持追加与自动创建目录",
    inputs={
        "path": File(
            picker="save",
            required=True,
            label="文件路径",
            placeholder="out/result.txt",
            help="相对路径按运行工作目录解析",
        ),
        "content": Text(
            required=True,
            label="内容",
        ),
        "encoding": Enum(
            ["utf-8", "utf-8-sig", "gbk"],
            default="utf-8",
            label="编码",
            help="给 Excel 看的 CSV 常用 utf-8-sig",
        ),
        "append": Bool(default=False, label="追加模式", help="打开则写在文件末尾，不覆盖"),
        "create_dirs": Bool(default=True, label="自动创建目录"),
    },
    outputs={
        "path": String(label="实际写入路径"),
        "bytes": Integer(label="写入字节数"),
        "lines": Integer(label="行数"),
        "created": Bool(label="是否新建了文件"),
    },
)
def write(
    ctx: Context,
    path: str,
    content: str,
    encoding: str = "utf-8",
    append: bool = False,
    create_dirs: bool = True,
) -> dict:
    """把文本写入文件。"""
    target = ctx.resolve(path)
    existed = target.exists()

    if create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.is_dir():
        raise FileNotFoundError(f"目录不存在：{target.parent}（可开启「自动创建目录」）")

    if target.is_dir():
        raise IsADirectoryError(f"{target} 是一个目录，不是文件")

    # 先编码再写盘：编码失败时（比如 gbk 存不下的字符）不会留下半个空文件。
    try:
        payload = content.encode(encoding)
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"内容无法用 {encoding} 编码（位置 {exc.start}）：{exc.reason}。"
            "含中文时建议改用 utf-8。"
        ) from exc

    mode = "ab" if append else "wb"
    with target.open(mode) as handle:
        handle.write(payload)

    lines = len(content.splitlines())
    action_text = "追加" if append else "写入"
    ctx.info(f"{action_text} {len(payload)} 字节 / {lines} 行 -> {target}")

    return {
        "path": str(target),
        "bytes": len(payload),
        "lines": lines,
        "created": not existed,
    }
