"""截屏与模板匹配。

图像匹配是定位降级链的第四级 —— 前面三级（AutomationId / 名称+类型 / 路径）都失效时
才轮到它。它有两个先天缺点：**慢**，而且对分辨率、系统主题、显示缩放都敏感。

所以这里做了两件事，让它从"不可用"变成"可以作为兜底"：

1. **不在全屏里找。** 拾取的时候记下了元素的矩形，匹配只在那个矩形附近的一块区域里
   进行 —— 窗口挪了一点、布局动了一点，都还在搜索范围内。
2. **由粗到细。** 先在缩小若干倍的图上滑一遍找到大致位置，再在附近按原尺寸精调。
   直接原尺寸全区域滑动是几千万次比较，用户会以为软件卡死了。

匹配得分是"每像素每通道的平均绝对差"（0 表示一模一样）。阈值默认 18/255，大致对应
"人眼看着还是那块东西，但允许有抗锯齿和轻微渲染差异"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageGrab, ImageStat

__all__ = ["grab", "capture", "find", "find_in", "DEFAULT_THRESHOLD"]

#: 平均绝对差超过这个值就认为不是同一个东西。
DEFAULT_THRESHOLD = 18.0

#: 粗找时最多缩小几倍。实际倍数还会受模板尺寸限制，见 ``_coarse_factor``。
_COARSE = 4

#: 缩小后模板至少还要有这么多像素，否则粗找会被别处的相似区域骗走。
_MIN_COARSE_HEIGHT = 16
_MIN_COARSE_WIDTH = 48

#: 在记录下来的矩形四周再往外找多少像素。
_SEARCH_PAD = 120

#: 一轮最多比较多少个位置 —— 防止在一个空旷的大区域里滑到天荒地老。
_MAX_POSITIONS = 60000


def grab() -> Image.Image:
    """抓一张主屏截图（RGB）。"""
    return ImageGrab.grab().convert("RGB")


def _coordinate_scale(image: Image.Image) -> tuple[float, float]:
    """截图相对**本进程坐标系**的缩放比。

    正常情况是 1.0（进程开了 DPI 感知，见 ``dpi.enable()``）。如果进程没开而显示器有
    缩放，截图是物理像素、坐标是虚拟像素，两者差一个缩放比 —— 这时必须换算，否则按
    坐标裁出来的是屏幕上另一块地方。这是实测踩出来的：125% 缩放下 2560/2048 = 1.25。
    """
    from .dpi import screen_size  # noqa: PLC0415 - 避免模块级循环依赖

    width, height = screen_size()
    if width <= 0 or height <= 0:
        return (1.0, 1.0)
    return (image.width / width, image.height / height)


def capture(rect: Any, path: str | Path) -> Path:
    """把屏幕上 ``rect`` 那块截下来存成 PNG。拾取元素时留作图像匹配的模板。"""
    left, top, right, bottom = (int(v) for v in rect)
    if right <= left or bottom <= top:
        raise ValueError(f"矩形是空的：{rect}")

    image = grab()
    scale_x, scale_y = _coordinate_scale(image)

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.crop(
        (
            round(left * scale_x),
            round(top * scale_y),
            round(right * scale_x),
            round(bottom * scale_y),
        )
    ).save(target)
    return target


def _mean_abs_diff(area: Image.Image, template: Image.Image, x: int, y: int) -> float:
    """把模板盖在 ``(x, y)`` 上，算平均绝对差。"""
    patch = area.crop((x, y, x + template.width, y + template.height))
    difference = ImageChops.difference(patch, template)
    stat = ImageStat.Stat(difference)
    pixels = template.width * template.height
    bands = len(stat.sum) or 1
    return sum(stat.sum) / (pixels * bands)


def _scan(
    area: Image.Image, template: Image.Image, origin: tuple[int, int], limit: int
) -> tuple[int, int, float] | None:
    """在 ``area`` 里逐点滑动模板，返回 ``(屏幕x, 屏幕y, 得分)``。

    ``origin`` 是 ``area`` 左上角在屏幕上的坐标 —— 所有返回值都换算回屏幕坐标，
    免得调用方还要记着自己在哪一层。
    """
    max_x = area.width - template.width
    max_y = area.height - template.height
    if max_x < 0 or max_y < 0:
        return None

    step = 1
    positions = (max_x // step + 1) * (max_y // step + 1)
    if positions > limit:
        # 位置太多就跳着扫。粗找阶段本来就允许不准，精调会补回来。
        step = max(1, int((positions / limit) ** 0.5) + 1)

    best: tuple[int, int, float] | None = None
    for y in range(0, max_y + 1, step):
        for x in range(0, max_x + 1, step):
            score = _mean_abs_diff(area, template, x, y)
            if best is None or score < best[2]:
                best = (x, y, score)
                if score == 0:
                    break
    if best is None:
        return None
    return (origin[0] + best[0], origin[1] + best[1], best[2])


def _coarse_factor(template: Image.Image) -> int:
    """粗找缩小几倍。

    **不能无脑除以 4。** 一个 548×32 的扁平按钮缩到 137×8 之后就只剩一条灰边，屏幕上
    到处都"像"它，粗找必然被骗到别的地方，而精调只在错位置附近转，救不回来。实测就是
    这么翻的车：真实位置得分 0.0（完美匹配），粗找却报了 4.45 的另一个地方。

    所以按模板尺寸收着来：缩小后至少还要有 ``_MIN_COARSE_HEIGHT`` 像素高。
    """
    factor = _COARSE
    factor = min(factor, max(1, template.height // _MIN_COARSE_HEIGHT))
    factor = min(factor, max(1, template.width // _MIN_COARSE_WIDTH))
    return max(1, factor)


def find_in(
    image: Image.Image,
    template_path: str | Path,
    threshold: float = DEFAULT_THRESHOLD,
    region: Any = None,
) -> tuple[int, int, float] | None:
    """在一张**已经拿到的图**里找模板，返回 ``(中心x, 中心y, 得分)``；找不到返回 None。

    ``find()`` 是给 Windows 桌面写的：它自己调 ``grab()`` 截屏，还带一层 DPI 坐标换算。
    拿它去匹配模拟器截图会同时错两件事 —— 它截的是 Windows 桌面而不是模拟器，而且返回值
    会被 DPI 缩放污染。

    这个函数匹配的是调用方递进来的那张图，**返回值就是那张图自己的坐标系**。对 adb 截图来
    说，那正好是设备坐标，能直接交给 ``input tap`` —— 没有窗口、没有缩放、没有 DPI，
    图上量到多少就是设备上的多少。这是走 ADB 白拿的一个好处。

    ``region`` 可以收窄搜索范围（``(左, 上, 右, 下)``，这张图的坐标）。知道按钮大概在哪一片
    时给它，能省掉大半的扫描量。
    """
    template_file = Path(template_path)
    if not template_file.is_file():
        return None

    with Image.open(template_file) as handle:
        template = handle.convert("RGB")
    if template.width < 4 or template.height < 4:
        return None

    if region is not None and tuple(region) != (0, 0, 0, 0):
        # 裁剪框要夹在图片范围内，并且保证不比模板小 —— 否则 _scan 直接返回 None，
        # 调用方只会看到"没找到"，却不知道是自己框错了。
        area_region = (
            max(0, int(region[0])),
            max(0, int(region[1])),
            min(image.width, int(region[2])),
            min(image.height, int(region[3])),
        )
    else:
        area_region = (0, 0, image.width, image.height)

    if (
        area_region[2] - area_region[0] < template.width
        or area_region[3] - area_region[1] < template.height
    ):
        return None

    area = image.crop(area_region)

    # 粗找 -> 精调，和 find() 同一套（包括 _coarse_factor 那条"扁平模板不能缩太狠"的教训）。
    factor = _coarse_factor(template)
    small_area = area.resize(
        (max(1, area.width // factor), max(1, area.height // factor)), Image.BILINEAR
    )
    small_template = template.resize(
        (max(2, template.width // factor), max(2, template.height // factor)), Image.BILINEAR
    )

    candidate: tuple[int, int, float] | None = None
    if small_template.width < small_area.width and small_template.height < small_area.height:
        # **粗找的坐标是缩小后的格子，不是原图坐标。** origin 传 (0, 0) 拿纯格子坐标，
        # 再自己乘回 factor —— 之前这里直接把格子坐标当原图坐标用，2 倍缩小时精调会跑到
        # 一半的位置上找，永远找不到。两个单位混在一起是这段代码最容易错的地方。
        coarse = _scan(small_area, small_template, (0, 0), _MAX_POSITIONS)
        if coarse is not None:
            coarse_x = area_region[0] + coarse[0] * factor
            coarse_y = area_region[1] + coarse[1] * factor
            pad = factor * 2
            fine_region = (
                max(area_region[0], coarse_x - pad),
                max(area_region[1], coarse_y - pad),
                min(area_region[2], coarse_x + template.width + pad),
                min(area_region[3], coarse_y + template.height + pad),
            )
            fine_area = image.crop(fine_region)
            candidate = _scan(fine_area, template, fine_region[:2], _MAX_POSITIONS)
            if candidate is None and (
                0 <= coarse_x <= image.width - template.width
                and 0 <= coarse_y <= image.height - template.height
            ):
                # 精调区域退化了（比如裁剪框比模板还小）。退回粗找的位置，但**必须用原尺寸
                # 重新量一次分数** —— 缩小图上的误差和阈值根本不是一个尺度。
                candidate = (
                    coarse_x,
                    coarse_y,
                    _mean_abs_diff(image, template, coarse_x, coarse_y),
                )

    if candidate is None or candidate[2] > threshold:
        return None
    return (
        round(candidate[0] + template.width / 2),
        round(candidate[1] + template.height / 2),
        round(candidate[2], 2),
    )


def find(
    image_path: str | Path,
    near_rect: Any = None,
    threshold: float = DEFAULT_THRESHOLD,
    screen_size: Any = None,
) -> tuple[int, int, float] | None:
    """在屏幕上找模板图，返回 ``(中心x, 中心y, 得分)``（进程坐标系）；找不到返回 None。

    ``near_rect`` 是拾取时记下的元素矩形，既用来收窄搜索范围，也用来做"先看它原来在
    哪"这一步。``screen_size`` 是拾取时的屏幕尺寸 —— 分辨率变了说明环境变了，这时图像
    匹配的前提已经不成立，直接放弃比给出一个错答案好。
    """
    from .dpi import screen_size as current_screen  # noqa: PLC0415

    template_path = Path(image_path)
    if not template_path.is_file():
        return None

    with Image.open(template_path) as handle:
        template = handle.convert("RGB")
    if template.width < 4 or template.height < 4:
        return None

    screen = grab()
    # ``(0, 0)`` 是"没有提示"，不是"屏幕尺寸是 0×0"。空元组和零元组都要当成没给。
    hint = tuple(screen_size) if screen_size else (0, 0)
    if hint != (0, 0) and hint != tuple(current_screen()):
        return None

    scale_x, scale_y = _coordinate_scale(screen)

    # --- 先看"它原来在哪" ---
    # 绝大多数情况下元素根本没动。先原地量一次，既是最高效的答案，也顺带压住了低纹理
    # 模板被别处相似区域骗走的问题（相似区域的得分不会比原地更好）。
    seed: tuple[int, int, float] | None = None
    if near_rect is not None and tuple(near_rect) != (0, 0, 0, 0):
        seed_x = round(int(near_rect[0]) * scale_x)
        seed_y = round(int(near_rect[1]) * scale_y)
        if (
            0 <= seed_x <= screen.width - template.width
            and 0 <= seed_y <= screen.height - template.height
        ):
            seed = (seed_x, seed_y, _mean_abs_diff(screen, template, seed_x, seed_y))

    # --- 搜索区域（换算到截图坐标系） ---
    if near_rect is not None and tuple(near_rect) != (0, 0, 0, 0):
        left, top, right, bottom = (int(v) for v in near_rect)
        region = (
            max(0, round(left * scale_x) - _SEARCH_PAD),
            max(0, round(top * scale_y) - _SEARCH_PAD),
            min(screen.width, round(right * scale_x) + _SEARCH_PAD),
            min(screen.height, round(bottom * scale_y) + _SEARCH_PAD),
        )
    else:
        region = (0, 0, screen.width, screen.height)

    area = screen.crop(region)

    # --- 粗找：缩小若干倍滑一遍 ---
    factor = _coarse_factor(template)
    small_area = area.resize(
        (max(1, area.width // factor), max(1, area.height // factor)), Image.BILINEAR
    )
    small_template = template.resize(
        (max(2, template.width // factor), max(2, template.height // factor)), Image.BILINEAR
    )

    candidate: tuple[int, int, float] | None = None
    if small_template.width < small_area.width and small_template.height < small_area.height:
        # 同上：粗找给的是缩小后的格子坐标，要乘回 factor 才能当原图坐标用。
        coarse = _scan(small_area, small_template, (0, 0), _MAX_POSITIONS)
        if coarse is not None:
            coarse_x = region[0] + coarse[0] * factor
            coarse_y = region[1] + coarse[1] * factor
            pad = factor * 2
            fine_region = (
                max(region[0], coarse_x - pad),
                max(region[1], coarse_y - pad),
                min(region[2], coarse_x + template.width + pad),
                min(region[3], coarse_y + template.height + pad),
            )
            fine_area = screen.crop(fine_region)
            candidate = _scan(fine_area, template, fine_region[:2], _MAX_POSITIONS)
            if candidate is None and (
                0 <= coarse_x <= screen.width - template.width
                and 0 <= coarse_y <= screen.height - template.height
            ):
                candidate = (
                    coarse_x,
                    coarse_y,
                    _mean_abs_diff(screen, template, coarse_x, coarse_y),
                )

    # 原地量的那一次和滑出来的结果比一比。给原地一点点偏好（半个灰度级）——两个位置
    # 一样像的时候，选"它原来在哪"更可能对。
    choices: list[tuple[float, tuple[int, int, float]]] = []
    if candidate is not None:
        choices.append((candidate[2], candidate))
    if seed is not None:
        choices.append((seed[2] - 0.5, seed))
    if not choices:
        return None

    best = min(choices, key=lambda item: item[0])[1]
    if best[2] > threshold:
        return None
    # 换算回进程坐标系
    return (
        round((best[0] + template.width / 2) / scale_x),
        round((best[1] + template.height / 2) / scale_y),
        round(best[2], 2),
    )
