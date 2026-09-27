"""等待插件。

看着是个最没技术含量的插件，但它演示了两个必须成立的东西：

1. **输出能被下游引用。** ``slept`` / ``finished_at`` 会被写文件节点取用。
2. **进度能实时推回内核。** 等待是最典型的"看起来卡死"的操作，所以这里分片睡眠
   并持续上报进度。以后换成"等待元素出现"，机制完全一样 —— 用户能看见剩余时间，
   就知道程序还活着。
"""

from __future__ import annotations

import time
from datetime import datetime

from myautowork import Context, Number, String, action

#: 分片长度。越小进度越顺滑，但通知次数越多；0.1s 是个舒服的平衡。
_TICK = 0.1

#: 单次等待的上限，防止用户手滑填个 1e9。
_MAX_SECONDS = 3600.0


@action(
    id="sleep",
    name="等待",
    category="时间",
    icon="clock",
    description="等待指定的秒数，期间持续上报进度",
    inputs={
        "seconds": Number(
            default=1.0,
            minimum=0.0,
            maximum=_MAX_SECONDS,
            label="秒数",
            help="最多 3600 秒",
        ),
        "reason": String(
            default="",
            label="等待原因",
            placeholder="等界面加载",
            help="只写进日志，方便事后判断这个等待还有没有必要",
        ),
    },
    outputs={
        "slept": Number(label="实际等待秒数"),
        "started_at": String(label="开始时间"),
        "finished_at": String(label="结束时间"),
    },
)
def sleep(ctx: Context, seconds: float = 1.0, reason: str = "") -> dict:
    """等待指定的秒数。"""
    try:
        total = float(seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"秒数必须是数字，收到 {seconds!r}") from exc

    if total < 0:
        raise ValueError(f"秒数不能为负，收到 {total}")
    if total > _MAX_SECONDS:
        raise ValueError(f"秒数上限 {_MAX_SECONDS:g}，收到 {total}")

    started_at = datetime.now().isoformat(timespec="seconds")
    suffix = f"（{reason}）" if reason else ""
    ctx.info(f"等待 {total:g} 秒{suffix}")

    deadline = time.monotonic() + total
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_TICK, remaining))
        if total > 0:
            left = max(0.0, deadline - time.monotonic())
            ctx.progress(min(1.0, (total - left) / total), f"剩余 {left:.1f}s")

    finished_at = datetime.now().isoformat(timespec="seconds")
    ctx.info(f"等待结束，实际 {total:g} 秒")
    return {
        "slept": total,
        "started_at": started_at,
        "finished_at": finished_at,
    }
