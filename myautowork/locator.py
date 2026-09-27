"""界面元素的定位描述。

`Element` 类型的参数值就是这个 ``Locator`` 的字典形式。拾取器产出它，``win.uia`` 消费它。

**为什么要五级降级，而不是只存一个 AutomationId。** 界面是会变的：开发者随手改个控件名、
换个布局、甚至只是换了个语言，任何单一属性都可能失效。所以拾取的时候把能拿到的信息全记
下来，运行时按可靠性从高到低依次尝试，第一个命中的就用：

===================  ==========================================  ==========
级别                  依据                                        什么时候会失效
===================  ==========================================  ==========
``automation_id``    开发时写死的控件 id                            开发改了 id
``name_type``        可见文字 + 控件类型                            文案改了 / 多语言
``path``             从窗口往下的父子链（类型+名字+序号）             布局改了
``image``            截图匹配                                       分辨率/主题变了
``point``            绝对坐标                                       窗口一移动就废
===================  ==========================================  ==========

后两级是**兜底**：屏幕上的东西最终总能"在某个位置画着"，所以只要有坐标就一定能点到。
代价是脆 —— 所以它们排最后，并且界面上会明确标出来，让人知道自己选的是一条容易断的路。

**为什么不干脆只用坐标。** 因为一次自动化流程可能要跑几百遍、跨越好几次窗口大小变化。
能用 AutomationId 的时候就绝不该用坐标。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable

__all__ = ["Locator", "PathStep", "STRATEGY_LABELS", "STRATEGY_ORDER"]

#: 降级顺序：从最稳到最脆。
STRATEGY_ORDER: tuple[str, ...] = ("automation_id", "name_type", "path", "image", "point")

STRATEGY_LABELS: dict[str, str] = {
    "automation_id": "AutomationId",
    "name_type": "名称 + 控件类型",
    "path": "相对路径",
    "image": "图像匹配",
    "point": "坐标",
}

#: 这些级别一旦用上，就说明前面的都失效了 —— 界面上要显眼地提示。
FRAGILE_STRATEGIES = frozenset({"image", "point"})


@dataclass
class PathStep:
    """路径上的一级。``index`` 用来区分同一层里同类型同名的兄弟。"""

    control_type: str = ""
    name: str = ""
    index: int = 0

    def label(self) -> str:
        parts = [self.control_type or "?"]
        if self.name:
            parts.append(self.name)
        if self.index:
            parts.append(f"#{self.index}")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.control_type, "name": self.name, "index": self.index}

    @classmethod
    def from_dict(cls, data: Any) -> PathStep:
        if isinstance(data, str):  # 容忍手工写的简写
            return cls(control_type=data)
        if not isinstance(data, dict):
            return cls()
        return cls(
            control_type=str(data.get("type") or ""),
            name=str(data.get("name") or ""),
            index=int(data.get("index") or 0),
        )


@dataclass
class Locator:
    """一个界面元素的完整定位描述。"""

    #: 控件自身的 AutomationId —— 开发时写死的，最可靠。
    automation_id: str = ""
    #: 可见文字。
    name: str = ""
    #: UIA 控件类型名，如 ``ButtonControl``。
    control_type: str = ""
    #: Win32 类名，如 ``Edit``。UIA 拿不到东西时的补充。
    class_name: str = ""

    #: 所属窗口。定位永远先缩小到窗口范围，免得在全桌面里撞名。
    window_title: str = ""
    window_class: str = ""

    #: 从窗口往下的父子链。
    path: tuple[PathStep, ...] = ()

    #: 截图文件（相对工作目录）。图像匹配用。
    image: str = ""
    #: 截图时元素在屏幕上的矩形，以及屏幕尺寸 —— 分辨率变了就不该再信它。
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    screen: tuple[int, int] = (0, 0)

    #: 绝对坐标。最后一道兜底。
    point: tuple[int, int] = (0, 0)

    def __post_init__(self) -> None:
        self.path = tuple(
            step if isinstance(step, PathStep) else PathStep.from_dict(step) for step in self.path
        )

    # -- 降级链 ---------------------------------------------------------------

    def has(self, strategy: str) -> bool:
        if strategy == "automation_id":
            return bool(self.automation_id)
        if strategy == "name_type":
            return bool(self.name)
        if strategy == "path":
            return bool(self.path)
        if strategy == "image":
            return bool(self.image)
        if strategy == "point":
            return self.point != (0, 0)
        return False

    def available(self) -> list[str]:
        """按降级顺序列出这个定位器实际可用的级别。"""
        return [name for name in STRATEGY_ORDER if self.has(name)]

    @property
    def best(self) -> str:
        options = self.available()
        return options[0] if options else ""

    @property
    def is_fragile(self) -> bool:
        """只靠图像或坐标 —— 能用，但要知道它在走钢丝。"""
        return bool(self.available()) and set(self.available()) <= FRAGILE_STRATEGIES

    @property
    def is_empty(self) -> bool:
        return not self.available()

    # -- 展示 -----------------------------------------------------------------

    def describe(self) -> str:
        """给人看的一行摘要。"""
        if self.is_empty:
            return "（空定位）"
        identity = self.automation_id or self.name or (self.class_name or "")
        kind = self.control_type.replace("Control", "") if self.control_type else ""
        head = f"{kind} {identity}".strip() or "未命名元素"
        window = self.window_title or self.window_class
        return f"{head} @ {window}" if window else head

    def describe_strategies(self) -> str:
        labels = [STRATEGY_LABELS.get(name, name) for name in self.available()]
        return " → ".join(labels) if labels else "无"

    def to_dict(self) -> dict[str, Any]:
        """转成参数值。空字段不写进去 —— 存进 JSON 的每个键都该是有意义的。"""
        data: dict[str, Any] = {}
        if self.automation_id:
            data["automation_id"] = self.automation_id
        if self.name:
            data["name"] = self.name
        if self.control_type:
            data["control_type"] = self.control_type
        if self.class_name:
            data["class_name"] = self.class_name
        if self.window_title:
            data["window_title"] = self.window_title
        if self.window_class:
            data["window_class"] = self.window_class
        if self.path:
            data["path"] = [step.to_dict() for step in self.path]
        if self.image:
            data["image"] = self.image
        if self.rect != (0, 0, 0, 0):
            data["rect"] = list(self.rect)
        if self.screen != (0, 0):
            data["screen"] = list(self.screen)
        if self.point != (0, 0):
            data["point"] = list(self.point)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> Locator:
        """从参数值还原。对残缺/手写的数据尽量宽容 —— 抛异常没有意义，
        定位器不完整的时候应该走到"降级到坐标"这一步，而不是让整条流程崩掉。"""
        if isinstance(data, Locator):
            return data
        if not isinstance(data, dict):
            return cls()

        def _pair(value: Any, fallback: tuple[int, int]) -> tuple[int, int]:
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                try:
                    return (int(value[0]), int(value[1]))
                except (TypeError, ValueError):
                    return fallback
            return fallback

        def _quad(value: Any) -> tuple[int, int, int, int]:
            if isinstance(value, (list, tuple)) and len(value) >= 4:
                try:
                    return (int(value[0]), int(value[1]), int(value[2]), int(value[3]))
                except (TypeError, ValueError):
                    return (0, 0, 0, 0)
            return (0, 0, 0, 0)

        raw_path = data.get("path") or ()
        return cls(
            automation_id=str(data.get("automation_id") or ""),
            name=str(data.get("name") or ""),
            control_type=str(data.get("control_type") or ""),
            class_name=str(data.get("class_name") or data.get("class") or ""),
            window_title=str(data.get("window_title") or ""),
            window_class=str(data.get("window_class") or ""),
            path=tuple(
                PathStep.from_dict(step) for step in raw_path if step is not None
            ),
            image=str(data.get("image") or ""),
            rect=_quad(data.get("rect")),
            screen=_pair(data.get("screen"), (0, 0)),
            point=_pair(data.get("point"), (0, 0)),
        )

    @classmethod
    def from_point(cls, x: int, y: int) -> Locator:
        """只拿得到坐标时的最小定位器 —— 拾取器兜底用。"""
        return cls(point=(int(x), int(y)))

    def merged_with(self, other: Locator) -> Locator:
        """用 ``other`` 里的空字段补全自己（不覆盖已有的）。

        拾取器分层采集时用：先拿到 UIA 属性，再补上截图和坐标。
        """
        data = self.to_dict()
        for key, value in other.to_dict().items():
            if key not in data or not data[key]:
                data[key] = value
        return Locator.from_dict(data)

    def ancestry_labels(self, limit: int = 6) -> Iterable[str]:
        for step in list(self.path)[:limit]:
            yield step.label()
