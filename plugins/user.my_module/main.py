"""我的模块

写一句它是干什么的
"""

from __future__ import annotations

from myautowork import Context, String, action


@action(
    id="run",
    name="我的模块",
    category="自定义",
    description="写一句它是干什么的",
    inputs={
        "text": String(default="", label="输入"),
    },
    outputs={
        "text": String(label="输出"),
    },
)
def run(ctx: Context, text: str = "") -> dict:
    """写一句它是干什么的"""
    ctx.info("收到：%s" % text)
    return {"text": text}
