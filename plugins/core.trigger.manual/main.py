"""手动触发插件。

它是"一次性"触发器：内核在运行开始时调用它一次，拿到输出（开始时间等）后就继续
往下走。流程列表上点一下"运行"，走的就是这里。

定时、热键、文件变化这类**长驻**触发器以后会走另一条路径：内核启动它们之后，
它们自己推事件回来，而不是被调用一次。``@trigger`` 已经用 ``one_shot`` 把这两种
形态区分开了，到时候不用改契约。
"""

from __future__ import annotations

from datetime import datetime

from myautowork import Context, String, trigger


@trigger(
    id="start",
    name="手动触发",
    category="触发",
    icon="play",
    description="由用户点击或命令行启动工作流",
    inputs={
        "note": String(default="", label="备注", placeholder="这次运行是干什么的"),
    },
    outputs={
        "started_at": String(label="开始时间"),
        "note": String(label="备注"),
    },
)
def start(ctx: Context, note: str = "") -> dict:
    """启动一次工作流运行。"""
    started_at = datetime.now().isoformat(timespec="seconds")
    if note:
        ctx.info(f"手动触发（{note}）")
    else:
        ctx.info("手动触发")
    return {"started_at": started_at, "note": note}
