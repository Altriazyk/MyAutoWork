"""流程控制：条件、变量、日志、等待、尽头。

**为什么条件要拆成「左值 / 运算符 / 右值」三个输入。** 表达式语言（``{{ ... }}``）只做
**点分路径取值**（``$node.n2.count``、``$vars.token``），它**没有** ``>``、``==`` 这些
运算符（见 ``kernel/expr.py`` 的 ``lookup``）。所以比较必须由这个插件自己做：用户用表达式
把两边的值取过来，运算符我们来实现。

**「条件不成立」走 error 出口，但它不是出错。** 引擎给每个节点的执行出口只有
``success`` / ``error`` 两个（``kernel/graph.py`` 的 ``VALID_EXEC_PORTS``），没有自定义
端口名。所以「条件判断」的 error 出口含义是**条件为假**。抛的是 ``ConditionNotMet``，
内核只往日志写一行、不打栈 —— 否则每次走 false 分支都刷一屏，看起来像故障。

**循环怎么做。** 执行器支持环（有 ``max_steps`` 兜底，超了会提示"若这是有意为之的循环，
请调高 max_steps"）。所以：

    设变量 n = 0  ->  条件判断 n < 5  --success-->  正文  ->  设变量 n = n + 1  ->  回到条件判断
                                      --error-->    出口

把「设变量 n = n + 1」的 success 连回「条件判断」，就是一个五次循环。

**为什么没有"反复求值的等待"。** 参数是在动作**被调用之前**求值一次的，动作里拿不到
原始表达式，所以 ``wait_until(某项 > 3)`` 这种做不到 —— 表达式不会重算。等待只能基于
动作自己能反复检查的**外部状态**，也就是下面这两个（文件、进程）。
"""

from __future__ import annotations

import csv
import io
import math
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from myautowork import (
    Any_,
    Bool,
    ConditionNotMet,
    Context,
    Enum,
    ExpectedError,
    Integer,
    List_,
    Number,
    String,
    Text,
    action,
)

__all__ = [
    "if_",
    "set_var",
    "log",
    "end",
    "wait_file",
    "wait_process",
    "repeat",
    "for_each",
    "math_op",
    "string_op",
    "assert_",
    "fail",
    "random_wait",
    "list_op",
    "dict_op",
    "switch_op",
    "value_op",
]

_IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0

#: 运算符。用中文名 —— 属性面板上 ``>=`` 和 ``>`` 挨在一起很容易选错。
OPERATORS = [
    "等于",
    "不等于",
    "大于",
    "大于等于",
    "小于",
    "小于等于",
    "包含",
    "不包含",
    "为空",
    "不为空",
    "为真",
    "为假",
]

_ORDER = {"大于": ">", "大于等于": ">=", "小于": "<", "小于等于": "<="}


def _as_number(value: Any) -> float | None:
    """尽力转成数字。转不动返回 None —— **不要用 0 兜底**，那会把"这不是数字"悄悄变成
    "它是零"，然后 3 > 0 成立，条件静默地判错。"""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "none", "null")
    return bool(value)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _loose_equal(left: Any, right: Any) -> bool:
    """先按数字比，再按字符串比。

    ``"3"`` 和 ``3`` 应该相等 —— 不同插件把同一个数字给成字符串还是整数并不统一
    （``getprop`` 那种文本输出天生是字符串），为这个卡住用户没有意义。
    """
    a, b = _as_number(left), _as_number(right)
    if a is not None and b is not None:
        return a == b
    return str(left).strip() == str(right).strip()


def compare(left: Any, op: str, right: Any) -> bool:
    """判定一个条件。这是这个插件的心脏，单独拆出来是为了能直接测。"""
    if op in ("为空", "不为空"):
        return _is_empty(left) if op == "为空" else not _is_empty(left)
    if op in ("为真", "为假"):
        # 只用一个值，右边的输入被忽略 —— 说明里写清楚了。
        return _truthy(left) if op == "为真" else not _truthy(left)
    if op in ("包含", "不包含"):
        if isinstance(left, (list, tuple, set)):
            hit = right in left
        elif isinstance(left, dict):
            hit = right in left
        else:
            hit = str(right) in str(left)
        return hit if op == "包含" else not hit
    if op in _ORDER:
        a, b = _as_number(left), _as_number(right)
        if a is None or b is None:
            raise ConditionNotMet(
                f"「{op}」两边都要是数字，但左边是 {left!r}、右边是 {right!r}，转不过去。"
                "是不是取错了端口，或者该用「等于」？"
            )
        return {
            ">": a > b,
            ">=": a >= b,
            "<": a < b,
            "<=": a <= b,
        }[_ORDER[op]]
    if op == "等于":
        return _loose_equal(left, right)
    if op == "不等于":
        return not _loose_equal(left, right)
    raise ConditionNotMet(f"不认识的运算符：{op!r}")


@action(
    id="if",
    name="条件判断",
    category="流程控制",
    icon="git-branch",
    description=(
        "比一下两个值。成立走 success 出口，不成立走 **error 出口** —— "
        "那是「条件为假」，不是出错。用「为真 / 为假」时右边不用填"
    ),
    inputs={
        "left": Any_(required=True, label="左边的值", help="通常是一个 {{ }} 表达式，比如 {{ $node.n2.count }}"),
        "op": Enum(OPERATORS, default="等于", label="比较方式"),
        "right": Any_(label="右边的值", help="可以是固定值，也可以是表达式。「为真 / 为假」时忽略"),
    },
    outputs={
        "left": Any_(label="左边的值"),
        "op": String(label="比较方式"),
        "right": Any_(label="右边的值"),
    },
)
def if_(
    ctx: Context,
    left: Any = None,
    op: str = "等于",
    right: Any = None,
    **_: Any,
) -> dict[str, Any]:
    passed = compare(left, op, right)
    shown = op if op in ("为空", "不为空", "为真", "为假") else f"{left!r} {op} {right!r}"
    if passed:
        ctx.info(f"条件成立：{shown}")
        return {"left": left, "op": op, "right": right}

    # 抛的是 ConditionNotMet：内核只写一行日志，不打栈。每次走 false 分支刷一屏栈
    # 会把真正的问题埋掉。
    raise ConditionNotMet(f"条件不成立：{shown}")


@action(
    id="set_var",
    name="设置变量",
    category="流程控制",
    icon="variable",
    description="写入一个工作流变量，后面的节点用 {{ $vars.名字 }} 取。也可以拿来做计数器",
    inputs={
        "name": String(required=True, label="变量名", help="字母数字下划线，比如 count"),
        "value": Any_(label="值"),
        "mode": Enum(
            ["设置", "相加", "追加"],
            default="设置",
            label="方式",
            help="相加 = 累加到原值上（做循环计数用）；追加 = 接到原文本后面",
        ),
    },
    outputs={"name": String(label="变量名"), "value": Any_(label="写进去的值")},
)
def set_var(
    ctx: Context,
    name: str,
    value: Any = None,
    mode: str = "设置",
    **_: Any,
) -> dict[str, Any]:
    key = (name or "").strip()
    if not key:
        raise ValueError("变量名不能是空的")

    if mode == "相加":
        current = _as_number(ctx.get_var(key))
        step = _as_number(value)
        if step is None:
            raise ValueError(f"「相加」要求值是个数字，收到的是 {value!r}")
        # 变量还没设过时按 0 起算 —— 否则第一次相加会拿到 None + 1 直接炸。
        result: Any = (current or 0.0) + step
        # 整数运算就还它一个整数，免得界面上显示成 3.0 让人以为哪里错了。
        if float(result).is_integer():
            result = int(result)
    elif mode == "追加":
        result = str(ctx.get_var(key) or "") + str(value if value is not None else "")
    else:
        result = value

    ctx.set_var(key, result)
    ctx.info(f"$vars.{key} = {result!r}")
    return {"name": key, "value": result}


@action(
    id="log",
    name="写日志",
    category="流程控制",
    icon="message-square",
    description="往运行日志里写一条。调试流程时最有用的一步 —— 比对着画布猜快得多",
    inputs={
        "message": Text(required=True, label="内容", help="可以直接嵌表达式，比如 当前 {{ $vars.count }}"),
        "level": Enum(["debug", "info", "warning", "error"], default="info", label="级别"),
    },
    outputs={"message": String(label="写出去的内容")},
)
def log(ctx: Context, message: Any = "", level: str = "info", **_: Any) -> dict[str, Any]:
    text = "" if message is None else str(message)
    emit = getattr(ctx, level, None)
    if callable(emit):
        emit(text)
    else:
        ctx.info(text)
    return {"message": text}


@action(
    id="end",
    name="结束",
    category="流程控制",
    icon="square",
    description=(
        "这条分支到此为止的**标记**。它什么都不做 —— 因为后面没连任何节点，流程自然就停了。"
        "放在画布上是为了让人一眼看出「这里是有意结束的」，而不是忘了连"
    ),
    inputs={"note": String(default="", label="备注", help="只写进日志，方便日后回看")},
    outputs={"note": String(label="备注")},
)
def end(ctx: Context, note: str = "", **_: Any) -> dict[str, Any]:
    ctx.info(f"流程到此结束{('：' + note) if note else ''}")
    return {"note": note}


@action(
    id="wait_file",
    name="等文件出现",
    category="流程控制",
    icon="file-clock",
    description="反复检查一个路径，直到它出现。**比睡固定秒数靠谱** —— 上游快慢不影响结果",
    inputs={
        "path": String(required=True, label="文件或文件夹", help="支持 {{ }} 表达式"),
        "timeout": Number(default=30.0, label="最多等几秒"),
        "interval": Number(default=0.5, label="隔多久看一次"),
        "min_size": Integer(
            default=0,
            label="至少多少字节",
            help="防止「文件刚建出来还没写完」就被当成好了。大于 0 时才检查",
        ),
    },
    outputs={"path": String(label="路径"), "waited": Number(label="等了多久")},
)
def wait_file(
    ctx: Context,
    path: str,
    timeout: float = 30.0,
    interval: float = 0.5,
    min_size: int = 0,
    **_: Any,
) -> dict[str, Any]:
    target = Path(str(path))
    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    gap = max(0.05, interval)

    while True:
        if target.exists() and (min_size <= 0 or target.stat().st_size >= min_size):
            waited = time.monotonic() - started
            ctx.info(f"{target} 出现了（等了 {waited:.1f} 秒）")
            return {"path": str(target), "waited": round(waited, 2)}
        if time.monotonic() >= deadline:
            break
        time.sleep(gap)

    raise ConditionNotMet(
        f"等了 {timeout:.1f} 秒，{target} 还是没出现"
        + (f"（或者没到 {min_size} 字节）" if min_size > 0 else "")
    )


def _process_pids(name: str) -> list[int]:
    """按进程名查 PID。用 tasklist，不为这一件事引入 psutil。"""
    if not _IS_WINDOWS:
        return []
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids: list[int] = []
    for row in csv.reader(io.StringIO(result.stdout or "")):
        if len(row) >= 2 and row[0].strip().lower() == name.lower():
            try:
                pids.append(int(row[1]))
            except ValueError:
                continue
    return pids


@action(
    id="wait_process",
    name="等程序出现",
    category="流程控制",
    icon="activity",
    description="反复查一个进程名，直到它跑起来。**先启动再等它真的起来**，别用固定延时赌",
    inputs={
        "name": String(required=True, label="进程名", help="比如 MEmu.exe。不写 .exe 会自动补上"),
        "timeout": Number(default=30.0, label="最多等几秒"),
        "interval": Number(default=0.5, label="隔多久查一次"),
    },
    outputs={"pids": String(label="PID，逗号分隔"), "waited": Number(label="等了多久")},
)
def wait_process(
    ctx: Context,
    name: str,
    timeout: float = 30.0,
    interval: float = 0.5,
    **_: Any,
) -> dict[str, Any]:
    resolved = (name or "").strip()
    # 少了 .exe，tasklist 一个都匹配不上，而且**不报错** —— 只是永远等不到。
    if resolved and "." not in resolved:
        resolved += ".exe"
    if not resolved:
        raise ValueError("要给一个进程名，比如 MEmu.exe")

    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    gap = max(0.05, interval)

    while True:
        pids = _process_pids(resolved)
        if pids:
            waited = time.monotonic() - started
            joined = ",".join(str(p) for p in pids)
            ctx.info(f"{resolved} 起来了（PID {joined}，等了 {waited:.1f} 秒）")
            return {"pids": joined, "waited": round(waited, 2)}
        if time.monotonic() >= deadline:
            break
        time.sleep(gap)

    raise ConditionNotMet(f"等了 {timeout:.1f} 秒，{resolved} 还是没跑起来")


# --------------------------------------------------------------------------- #
# 循环
# --------------------------------------------------------------------------- #


@action(
    id="repeat",
    name="循环",
    category="流程控制",
    icon="repeat",
    description=(
        "计数循环的**循环头**。success 出口接正文，正文末尾连回本节点；"
        "数到上限就走 error 出口出去"
    ),
    inputs={
        "name": String(required=True, label="计数器变量名", help="比如 i，正文里用 {{ $vars.i }} 取"),
        "start": Number(default=0, label="从几开始"),
        "end": Number(default=10, label="到几为止（不含）"),
        "step": Number(default=1, label="每次加多少", help="可以是负数，那就是倒数"),
    },
    outputs={
        "value": Number(label="当前值"),
        "index": Integer(label="第几次（从 0 数）"),
        "count": Integer(label="总共几次"),
    },
)
def repeat(
    ctx: Context,
    name: str,
    start: float = 0,
    end: float = 10,
    step: float = 1,
    **_: Any,
) -> dict[str, Any]:
    """循环头：第一次把计数器设成 ``start``，之后每次调用加 ``step``，到 ``end`` 就走 error 出口。

    **为什么值得单独一个节点。** 用「条件判断 + 设置变量」手搭一个循环要两个节点加一根回边，
    而且"初始化"和"自增"是两件不同的事（一个在循环外、一个在循环尾），连错了很难一眼看出来。
    合成一个之后，画布上就是一个节点加一根回边，意图清楚得多。

    边界：``start=0, end=5`` 走 0/1/2/3/4 共 5 次 —— **不含 end**，和代码里的 range 一致。
    """
    key = (name or "").strip()
    if not key:
        raise ValueError("计数器变量名不能是空的")
    if step == 0:
        raise ValueError("步长不能是 0 —— 那样计数器永远到不了头，会一直转到步数上限")

    current = ctx.get_var(key)
    if current is None:
        value = float(start)
        first = True
    else:
        number = _as_number(current)
        if number is None:
            raise ValueError(
                f"计数器 {key} 现在的值是 {current!r}，不是数字 —— "
                "是不是有别的地方往同名变量里塞了别的东西？"
            )
        value = number + float(step)
        first = False

    done = value >= end if step > 0 else value <= end
    if done:
        # 走 error 出口。抛的是 ConditionNotMet，内核只写一行日志、不打栈。
        raise ConditionNotMet(f"循环结束：{key} 到了 {value:g}（上限 {end:g}）")

    # **整数要还它整数。** 内部用 float 算（步长可以是 0.5），但 0.0 拼进文件名会变成
    # "报告_0.0.txt" —— 用户看到的和想的不一样，而且很难往回查。
    if float(value).is_integer():
        value = int(value)
    ctx.set_var(key, value)
    count = max(1, int(abs((end - start) / step) + 0.999999))
    index = int(round(abs((value - start) / step)))
    ctx.info(f"循环 {index + 1}/{count}：{key} = {value:g}" + ("（第一次，已初始化）" if first else ""))
    return {"value": value, "index": index, "count": count}


def _as_list(value: Any, delimiter: str) -> list[Any]:
    """把输入变成列表。已经是列表就原样用，是文本就按分隔符切。"""
    if isinstance(value, (list, tuple)):
        return list(value)
    text = "" if value is None else str(value)
    if not delimiter:
        return [text] if text else []
    return text.split(delimiter)


@action(
    id="for_each",
    name="遍历列表",
    category="流程控制",
    icon="list",
    description=(
        "挨个取出列表里的元素。success 出口接正文，正文末尾连回本节点；"
        "取完了走 error 出口"
    ),
    inputs={
        "name": String(required=True, label="元素变量名", help="每轮把当前元素写进这个变量"),
        "items": Any_(
            label="列表",
            help="通常是一个 {{ }} 表达式取到的列表。给文本的话按下面的分隔符切开",
        ),
        "delimiter": String(
            default="\n",
            label="分隔符",
            help="列表是一段文本时按它切。默认按换行 —— 贴进来的一列东西正好是这个形状",
        ),
    },
    outputs={
        "item": Any_(label="当前元素"),
        "index": Integer(label="第几个（从 0 数）"),
        "count": Integer(label="一共几个"),
        "first": Bool(label="是不是第一个"),
        "last": Bool(label="是不是最后一个"),
    },
)
def for_each(
    ctx: Context,
    name: str,
    items: Any = None,
    delimiter: str = "\n",
    **_: Any,
) -> dict[str, Any]:
    """遍历列表的循环头。

    **列表只在第一轮固化一次。** 每一轮都重新读 ``items`` 的话，如果它是从上游节点的输出
    取的，而那个节点在循环里被再跑了一次，列表就可能变长 —— 循环永远走不完。第一轮存下来
    之后就认那一份，行为才可预测。
    """
    key = (name or "").strip()
    if not key:
        raise ValueError("元素变量名不能是空的")

    index_key = f"{key}__index"
    items_key = f"{key}__items"
    stored = ctx.get_var(items_key)

    if stored is None:
        sequence = _as_list(items, delimiter)
        ctx.set_var(items_key, sequence)
        index = 0
        if not sequence:
            raise ConditionNotMet("列表是空的，没东西可遍历")
    else:
        sequence = list(stored) if isinstance(stored, (list, tuple)) else [stored]
        index = int(_as_number(ctx.get_var(index_key)) or 0) + 1
        if index >= len(sequence):
            raise ConditionNotMet(f"列表遍历完了（共 {len(sequence)} 个）")

    ctx.set_var(index_key, index)
    item = sequence[index]
    ctx.set_var(key, item)
    ctx.info(f"遍历 {index + 1}/{len(sequence)}：{key} = {item!r}")
    return {
        "item": item,
        "index": index,
        "count": len(sequence),
        "first": index == 0,
        "last": index == len(sequence) - 1,
    }


# --------------------------------------------------------------------------- #
# 值的加工
# --------------------------------------------------------------------------- #

_MATH_OPS = ["加", "减", "乘", "除", "取余", "次方", "取较大", "取较小", "四舍五入", "向下取整", "向上取整", "绝对值"]


@action(
    id="math",
    name="数值运算",
    category="流程控制",
    icon="calculator",
    description="算一个数。需要多个步骤就串几个这个节点",
    inputs={
        "left": Any_(required=True, label="左值"),
        "op": Enum(_MATH_OPS, default="加", label="运算"),
        "right": Any_(label="右值", help="「绝对值」只用左值，右边不用填"),
        "digits": Integer(default=0, label="四舍五入保留几位"),
    },
    outputs={"result": Number(label="结果"), "text": String(label="结果的文本")},
)
def math_op(
    ctx: Context,
    left: Any = None,
    op: str = "加",
    right: Any = None,
    digits: int = 0,
    **_: Any,
) -> dict[str, Any]:
    """数值运算。**两边都要是数字**，转不动就明确报错。

    不拿 0 兜底：那会把"这不是数字"悄悄变成"它是零"，然后 3 + 0 = 3 一路传下去，
    等到很久之后某个地方结果不对，已经查不回源头了。
    """
    a = _as_number(left)
    if a is None:
        raise ValueError(f"左值 {left!r} 不是数字")

    if op == "绝对值":
        result = abs(a)
    elif op in ("四舍五入", "向下取整", "向上取整"):
        if op == "四舍五入":
            result = round(a, int(digits))
        elif op == "向下取整":
            result = math.floor(a)
        else:
            result = math.ceil(a)
    else:
        b = _as_number(right)
        if b is None:
            raise ValueError(f"「{op}」需要右值，但 {right!r} 不是数字")
        if op == "加":
            result = a + b
        elif op == "减":
            result = a - b
        elif op == "乘":
            result = a * b
        elif op == "除":
            if b == 0:
                raise ValueError("除数不能是 0")
            result = a / b
        elif op == "取余":
            if b == 0:
                raise ValueError("取余的除数不能是 0")
            result = a % b
        elif op == "次方":
            result = a**b
        elif op == "取较大":
            result = max(a, b)
        else:
            result = min(a, b)

    # 整数就还它一个整数，免得界面上显示 3.0 让人以为哪里错了。
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    text = f"{result:g}" if isinstance(result, float) else str(result)
    ctx.info(f"{left!r} {op} {right!r} = {text}")
    return {"result": result, "text": text}


_STRING_OPS = [
    "去首尾空白",
    "去所有空白",
    "转大写",
    "转小写",
    "取长度",
    "替换",
    "取前 N 个",
    "取后 N 个",
    "按分隔符取第 N 段",
    "拼接",
    "包含吗",
    "以…开头吗",
    "以…结尾吗",
    "正则提取",
]


@action(
    id="string",
    name="文本处理",
    category="流程控制",
    icon="type",
    description="切、拼、替换、正则。用来把上一步的输出整理成下一步要的形状",
    inputs={
        "text": Any_(required=True, label="文本"),
        "op": Enum(_STRING_OPS, default="去首尾空白", label="做什么"),
        "arg": String(default="", label="参数一", help="替换＝旧文本；取前/后 N 个＝N；按分隔符取＝第几段；正则提取＝模式"),
        "arg2": String(default="", label="参数二", help="替换＝新文本；拼接＝接在后面的内容；按分隔符取＝分隔符"),
    },
    outputs={"result": Any_(label="结果"), "text": String(label="结果的文本")},
)
def string_op(
    ctx: Context,
    text: Any = None,
    op: str = "去首尾空白",
    arg: str = "",
    arg2: str = "",
    **_: Any,
) -> dict[str, Any]:
    raw = "" if text is None else str(text)

    if op == "去首尾空白":
        result: Any = raw.strip()
    elif op == "去所有空白":
        result = "".join(raw.split())
    elif op == "转大写":
        result = raw.upper()
    elif op == "转小写":
        result = raw.lower()
    elif op == "取长度":
        result = len(raw)
    elif op == "替换":
        result = raw.replace(arg, arg2)
    elif op in ("取前 N 个", "取后 N 个"):
        count = _as_number(arg)
        if count is None:
            raise ValueError(f"「{op}」要在参数一里填个数，收到 {arg!r}")
        size = int(count)
        result = raw[:size] if op == "取前 N 个" else (raw[-size:] if size else "")
    elif op == "按分隔符取第 N 段":
        number = _as_number(arg)
        if number is None:
            raise ValueError(f"「按分隔符取第 N 段」要在参数一里填第几段，收到 {arg!r}")
        parts = raw.split(arg2 if arg2 else "\n")
        index = int(number)
        if not -len(parts) <= index < len(parts):
            raise ValueError(
                f"要第 {index} 段，但按 {arg2!r} 切开只有 {len(parts)} 段 —— "
                "段号从 0 数，也可以是负数（-1 是最后一段）"
            )
        result = parts[index]
    elif op == "拼接":
        result = raw + arg
    elif op == "包含吗":
        result = arg in raw
    elif op == "以…开头吗":
        result = raw.startswith(arg)
    elif op == "以…结尾吗":
        result = raw.endswith(arg)
    elif op == "正则提取":
        try:
            match = re.search(arg, raw)
        except re.error as exc:
            raise ValueError(f"正则写错了：{exc}") from exc
        if match is None:
            raise ConditionNotMet(f"正则 {arg!r} 在文本里没匹配到")
        # 有捕获组就取第一组，没有就取整个匹配 —— 这样两种写法都能用。
        result = match.group(1) if match.groups() else match.group(0)
    else:
        raise ValueError(f"不认识的文本操作：{op!r}")

    shown = result if isinstance(result, str) else str(result)
    ctx.info(f"{op}：{shown[:80]}" + ("…" if len(shown) > 80 else ""))
    return {"result": result, "text": shown}


# --------------------------------------------------------------------------- #
# 守卫
# --------------------------------------------------------------------------- #


@action(
    id="assert",
    name="断言",
    category="流程控制",
    icon="shield",
    description=(
        "条件不成立就**让整条流程停下**。跟「条件判断」的区别是它没有第二个出口 —— "
        "适合「走到这一步之前必须成立」的前置检查"
    ),
    inputs={
        "left": Any_(required=True, label="左边的值"),
        "op": Enum(OPERATORS, default="等于", label="比较方式"),
        "right": Any_(label="右边的值"),
        "message": Text(default="", label="不成立时说什么", help="留空会自动拼一句"),
    },
    outputs={"left": Any_(label="左边的值"), "right": Any_(label="右边的值")},
)
def assert_(
    ctx: Context,
    left: Any = None,
    op: str = "等于",
    right: Any = None,
    message: str = "",
    **_: Any,
) -> dict[str, Any]:
    if compare(left, op, right):
        ctx.info(f"断言通过：{left!r} {op} {right!r}")
        return {"left": left, "right": right}
    raise ExpectedError(message.strip() or f"断言不成立：{left!r} {op} {right!r}")


@action(
    id="fail",
    name="主动失败",
    category="流程控制",
    icon="alert",
    description="走到这里就报错。放在「按理说不该到这儿」的分支上，比如兜底的那个 else",
    inputs={"message": Text(required=True, label="说什么")},
    outputs={},
)
def fail(ctx: Context, message: str = "", **_: Any) -> dict[str, Any]:
    raise ExpectedError(message.strip() or "流程主动中止")


@action(
    id="random_wait",
    name="随机等待",
    category="流程控制",
    icon="dice",
    description=(
        "等一个**随机**时长。固定间隔的操作在游戏或风控眼里就是机器 —— "
        "同样两步之间等 1.00 秒和等 0.83～1.37 秒，看起来完全不一样"
    ),
    inputs={
        "min_seconds": Number(default=0.5, label="最短等几秒"),
        "max_seconds": Number(default=2.0, label="最长等几秒"),
    },
    outputs={"seconds": Number(label="实际等了多久")},
)
def random_wait(
    ctx: Context,
    min_seconds: float = 0.5,
    max_seconds: float = 2.0,
    **_: Any,
) -> dict[str, Any]:
    # 两个值给反了也认 —— 为这个报错纯属折腾用户。
    low, high = sorted((float(min_seconds), float(max_seconds)))
    if low < 0:
        raise ValueError("等待时间不能是负数")
    seconds = random.uniform(low, high)
    ctx.info(f"随机等 {seconds:.2f} 秒（区间 {low:g}～{high:g}）")
    time.sleep(seconds)
    return {"seconds": round(seconds, 3)}


# --------------------------------------------------------------------------- #
# 列表和字典
# --------------------------------------------------------------------------- #

_LIST_OPS = [
    "从文本切",
    "有几个",
    "取第 N 个",
    "取一段",
    "接在末尾",
    "去掉第 N 个",
    "排序",
    "去重",
    "反转",
    "求和",
    "最大值",
    "最小值",
    "包含吗",
    "是第几个",
    "拼成文本",
]


def _as_dict(value: Any) -> dict[str, Any]:
    """把输入变成字典。已经是字典就原样用；是一段 ``键=值`` 的文本就解析。"""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        out: dict[str, Any] = {}
        for line in value.splitlines() or [value]:
            line = line.strip()
            if not line:
                continue
            key, sep, raw = line.partition("=")
            if not sep:
                continue
            out[key.strip()] = raw.strip()
        return out
    return {}


def _index_of(arg: Any, length: int, what: str) -> int:
    """把参数解析成下标。**序号从 0 数**，负数从末尾数 —— 和代码里的习惯一致。"""
    number = _as_number(arg)
    if number is None:
        raise ValueError(f"「{what}」要在参数一里填个序号，收到 {arg!r}")
    index = int(number)
    if not -length <= index < length:
        raise ValueError(
            f"要第 {index} 个，但一共只有 {length} 个 —— "
            "序号从 0 数，也可以是负数（-1 是最后一个）"
        )
    return index


@action(
    id="list",
    name="列表处理",
    category="流程控制",
    icon="list",
    description=(
        "切分、取用、拼接、排序、去重。**返回的是新列表，不改输入** —— "
        "想存下来就再接一个「设置变量」"
    ),
    inputs={
        "items": Any_(label="列表", help="已经是列表就用它；是一段文本就按下面的分隔符切开"),
        "op": Enum(_LIST_OPS, default="从文本切", label="做什么"),
        "arg": String(default="", label="参数一", help="序号、要接上去的值、或要查找的东西"),
        "arg2": String(default="", label="参数二", help="取一段时的结束序号、拼成文本时的连接符"),
        "delimiter": String(default="\n", label="分隔符", help="文本按它切。默认按换行"),
    },
    outputs={
        "result": Any_(label="结果（列表或单个值）"),
        "text": String(label="文本形式"),
        "count": Integer(label="元素个数"),
    },
)
def list_op(
    ctx: Context,
    items: Any = None,
    op: str = "从文本切",
    arg: str = "",
    arg2: str = "",
    delimiter: str = "\n",
    **_: Any,
) -> dict[str, Any]:
    """列表处理。

    **返回新列表，不改输入。** 引擎里每个节点都是"算出一个值交给下游"，就地修改会让
    "这个变量现在是什么"依赖执行顺序 —— 那是很难查的一类问题。要留下来就接
    「设置变量」，一眼能看出写到了哪。
    """
    current = _as_list(items, delimiter)

    if op == "从文本切":
        result: Any = current
    elif op == "有几个":
        result = len(current)
    elif op == "取第 N 个":
        result = current[_index_of(arg, len(current), op)]
    elif op == "取一段":
        start = _index_of(arg, len(current) + 1, op)
        end = _as_number(arg2)
        result = current[start : int(end) if end is not None else None]
    elif op == "接在末尾":
        result = current + [arg]
    elif op == "去掉第 N 个":
        index = _index_of(arg, len(current), op)
        result = current[:index] + current[index + 1 :]
    elif op == "排序":
        try:
            result = sorted(current)
        except TypeError as exc:
            raise ValueError(f"排序失败：列表里混了没法互相比较的东西（{exc}）") from exc
    elif op == "去重":
        # 用 dict 保序，不用 set —— set 会把顺序打乱，而顺序往往是有意义的。
        result = list(dict.fromkeys(current))
    elif op == "反转":
        result = list(reversed(current))
    elif op in ("求和", "最大值", "最小值"):
        numbers = [_as_number(item) for item in current]
        if not numbers or any(n is None for n in numbers):
            bad = [item for item, n in zip(current, numbers) if n is None]
            raise ValueError(f"「{op}」要求全是数字，但这些不是：{bad[:5]}")
        result = {"求和": sum, "最大值": max, "最小值": min}[op](numbers)  # type: ignore[arg-type]
        if float(result).is_integer():
            result = int(result)
    elif op == "包含吗":
        result = arg in current or arg in [str(item) for item in current]
    elif op == "是第几个":
        try:
            result = current.index(arg)
        except ValueError:
            raise ConditionNotMet(f"列表里没有 {arg!r}") from None
    elif op == "拼成文本":
        result = (arg2 or "\n").join(str(item) for item in current)
    else:
        raise ValueError(f"不认识的列表操作：{op!r}")

    if isinstance(result, list):
        count = len(result)
        text = (arg2 or "\n").join(str(item) for item in result)
    else:
        count = len(current)
        text = str(result)
    ctx.info(f"{op} -> {text[:80]}" + ("…" if len(text) > 80 else ""))
    return {"result": result, "text": text, "count": count}


@action(
    id="value",
    name="值",
    category="流程控制",
    icon="hash",
    description=(
        "画布上的一个常量。用连线把它喂给下游的输入 —— "
        "**比「存进变量再读出来」直观**：值在画布上看得见，同一个值还能喂给好几处"
    ),
    inputs={
        "value": Text(label="值", help="直接填。列表一行一条，字典填 JSON"),
        "type": Enum(
            ["文本", "数字", "是/否", "列表", "字典"],
            default="文本",
            label="当什么用",
        ),
    },
    outputs={
        "value": Any_(label="值"),
        "text": String(label="文本形式"),
    },
)
def value_op(
    ctx: Context, value: str = "", type: str = "文本", **_: Any
) -> dict[str, Any]:
    """把一段文字变成一个**有类型**的值。

    **为什么是"填文字 + 选类型"，而不是让它自己猜。**
    猜的话：``123`` 是数字还是文本？``是`` 是布尔还是那两个字？``[1,2]`` 是列表还是
    四个字符？每种猜法都会在某些场景下错，而用户没法纠正 —— 它看起来只是"值传错了"。
    让他明说一次，比事后查半天强得多。
    """
    raw = value if isinstance(value, str) else str(value)

    if type == "数字":
        try:
            number = float(raw.strip())
        except ValueError:
            raise ValueError(f"「值」填的是 {raw.strip()!r}，那不是一个数字") from None
        parsed: Any = int(number) if number.is_integer() else number
    elif type == "是/否":
        token = raw.strip().casefold()
        if token in ("是", "真", "true", "1", "yes", "y", "开"):
            parsed = True
        elif token in ("否", "假", "false", "0", "no", "n", "关", ""):
            parsed = False
        else:
            raise ValueError(f"「是/否」认不出 {raw.strip()!r} —— 填「是」或「否」")
    elif type == "列表":
        # 一行一条。空行去掉 —— 在这里空行只会是排版，不可能是"一个空元素"。
        parsed = [line for line in raw.splitlines() if line.strip()]
    elif type == "字典":
        import json  # noqa: PLC0415

        try:
            parsed = json.loads(raw)
        except Exception as exc:
            raise ValueError(f"「字典」那栏不是合法的 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"「字典」要一个 JSON 对象，收到 {type(parsed).__name__}")
    else:
        parsed = raw

    ctx.info(f"值 = {parsed!r}"[:120])
    return {"value": parsed, "text": str(parsed)}


@action(
    id="switch",
    name="多路分支",
    category="流程控制",
    icon="git-branch",
    description=(
        "按值选一条出口走。**分支名本身就是要比对的值** —— "
        "四路分支是 1 个节点 + 4 条边，不用串三个「条件判断」"
    ),
    branches="cases",
    inputs={
        "value": Any_(label="按什么分", help="拿它的值去和每条分支比"),
        "cases": Text(
            label="分支",
            help="一行一条。每一行既是画布上那个出口的名字，也是要比对的值",
            placeholder="批发\n零售\n退单\n其他",
        ),
        "fallback": String(
            default="",
            label="都不匹配时走哪条",
            help="填一条上面已有的分支名；留空表示没有匹配就按失败处理",
        ),
        "ignore_case": Bool(default=False, label="忽略大小写", help="比对文本时用"),
    },
    outputs={
        "matched": String(label="走了哪条分支"),
        "text": String(label="值的文本形式"),
    },
)
def switch_op(
    ctx: Context,
    value: Any = None,
    cases: Any = None,
    fallback: str = "",
    ignore_case: bool = False,
    **_: Any,
) -> dict[str, Any]:
    """多路分支。分支名既是出口名，也是要比对的值。

    **为什么让分支名兼任比对值。** 另一种做法是"条件列表 + 分支名列表按位置对应"，
    但那样一改顺序就全错位了，而且用户改完看不出来。合成一个之后，画布上写的是
    ``批发 / 零售 / 退单``，既是出口又是判据，读起来就是人话。
    """
    # 空行要去掉。``_as_list("")`` 会给出 ``[""]`` 而不是 ``[]`` —— 不过滤的话
    # "一条分支都没填"会被当成"有一条叫空字符串的分支"，报出来的错完全指错方向。
    names = [str(item).strip() for item in _as_list(cases, "\n") if str(item).strip()]
    if not names:
        raise ValueError("「多路分支」至少要有一条分支，去属性面板里加")

    def same(left: Any, right: Any) -> bool:
        if ignore_case and isinstance(left, str) and isinstance(right, str):
            return left.casefold() == right.casefold()
        return _loose_equal(left, right)

    for name in names:
        if same(value, name):
            ctx.info(f"走分支「{name}」")
            return {"branch": name, "matched": name, "text": str(value)}

    # 没匹配上的处理。**默认报错而不是悄悄走第一条** —— 悄悄走会让"分支写错了"
    # 表现成"流程莫名走到了别的地方"，而报错会直接告诉你值是什么、有哪些分支。
    if not fallback:
        raise ConditionNotMet(
            f"没有一条分支匹配 {value!r}。现有的分支：{'、'.join(str(n) for n in names)}。"
            "想让不匹配时也有地方去，就在「都不匹配时走哪条」里填一条分支名"
        )
    if fallback not in names:
        raise ValueError(
            f"「都不匹配时走哪条」填的是 {fallback!r}，但它不在分支列表里："
            f"{'、'.join(str(n) for n in names)}"
        )
    ctx.info(f"没有匹配，走兜底分支「{fallback}」")
    return {"branch": fallback, "matched": fallback, "text": str(value)}


_DICT_OPS = [
    "取出某个键",
    "设一个键",
    "删掉某个键",
    "有哪些键",
    "有哪些值",
    "有几个键",
    "有这个键吗",
    "合并另一个字典",
    "从键值对文本构造",
]


@action(
    id="dict",
    name="字典处理",
    category="流程控制",
    icon="braces",
    description=(
        "结构化数据的读和改。**同样返回新的，不改输入**。"
        "从一段「键=值」的文本构造字典也在这儿"
    ),
    inputs={
        "mapping": Any_(label="字典", help="已经是字典就用它；是「键=值」每行一条的文本就解析"),
        "op": Enum(_DICT_OPS, default="取出某个键", label="做什么"),
        "key": String(default="", label="键"),
        "value": Any_(label="值", help="「设一个键」时写进去的东西"),
        "delimiter": String(default="\n", label="分隔符", help="把键或值拼成文本时用"),
    },
    outputs={
        "result": Any_(label="结果（字典或单个值）"),
        "text": String(label="文本形式"),
        "count": Integer(label="键的个数"),
    },
)
def dict_op(
    ctx: Context,
    mapping: Any = None,
    op: str = "取出某个键",
    key: str = "",
    value: Any = None,
    delimiter: str = "\n",
    **_: Any,
) -> dict[str, Any]:
    """字典处理。和列表一样**返回新的**，不改输入。"""
    current = _as_dict(mapping)

    if op == "从键值对文本构造":
        result: Any = current
    elif op == "取出某个键":
        if key not in current:
            available = "、".join(sorted(str(k) for k in current)) or "（空字典）"
            # 取不到就走 error 出口，而不是给个空值 —— 让"这里没取到"在画布上看得见。
            raise ConditionNotMet(f"字典里没有键 {key!r}。现有的键：{available}")
        result = current[key]
    elif op == "设一个键":
        if not key:
            raise ValueError("「设一个键」要在「键」里填个名字")
        result = {**current, key: value}
    elif op == "删掉某个键":
        result = {k: v for k, v in current.items() if k != key}
    elif op == "有哪些键":
        result = list(current.keys())
    elif op == "有哪些值":
        result = list(current.values())
    elif op == "有几个键":
        result = len(current)
    elif op == "有这个键吗":
        result = key in current
    elif op == "合并另一个字典":
        other = _as_dict(value)
        if not other:
            raise ValueError("「合并」要在「值」里给另一个字典（或一段「键=值」的文本）")
        result = {**current, **other}
    else:
        raise ValueError(f"不认识的字典操作：{op!r}")

    if isinstance(result, dict):
        count = len(result)
        text = delimiter.join(f"{k}={v}" for k, v in result.items())
    elif isinstance(result, list):
        count = len(current)
        text = delimiter.join(str(item) for item in result)
    else:
        count = len(current)
        text = str(result)
    ctx.info(f"{op} -> {text[:80]}" + ("…" if len(text) > 80 else ""))
    return {"result": result, "text": text, "count": count}
