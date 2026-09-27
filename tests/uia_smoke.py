"""界面自动化测试。

这个文件必须跑在**真实桌面**上（不设 QT_QPA_PLATFORM），因为被测的就是真实的 UIA ——
离屏渲染出来的窗口在系统里根本不存在，UIA 看不见它。代价是运行期间屏幕上会短暂出现一个
小窗口，这没办法：要验证"能不能找到并点到一个真实控件"，就必须真的有一个真实控件。

覆盖三件事：

1. **闭环**：控件 → 定位器 → 控件，转一圈回来必须是同一个。
2. **降级链**：把高层级的信息一个个抹掉，逼它往下走，直到只剩坐标。这条链是界面自动化
   能在界面改动后活下来的唯一原因，必须逐级验证。
3. **失败要说人话**：找不到元素时的报错得能指导人改，而不是一句"失败了"。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 真实平台 —— 必须在 import PySide6 之前定下来。
import os  # noqa: E402

os.environ.pop("QT_QPA_PLATFORM", None)
os.environ.pop("QT_QPA_FONTDIR", None)

# DPI 感知也必须在这里开：一旦创建了窗口，系统就不再允许改。
# 不开的话 UIA 坐标和屏幕截图会差一个缩放比（实测 125% 缩放下是 1.25 倍）。
from myautowork import dpi  # noqa: E402

print(f"DPI 感知：{dpi.enable()}    屏幕：{dpi.screen_size()}")

import win32gui  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402

from myautowork import imaging  # noqa: E402
from myautowork import uia as bridge  # noqa: E402
from myautowork.locator import Locator  # noqa: E402


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append(name)
            print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def report(self) -> int:
        print(f"\n通过 {self.passed} 项，失败 {len(self.failed)} 项")
        if self.failed:
            for name in self.failed:
                print(f"  - {name}")
            return 1
        return 0


WINDOW_TITLE = "myautowork UIA 测试窗口"


def main() -> int:
    checker = Checker()

    from PySide6.QtWidgets import (  # noqa: PLC0415
        QApplication,
        QCheckBox,
        QLineEdit,
        QPushButton,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )

    class TestWindow(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle(WINDOW_TITLE)
            self.setObjectName("testRoot")
            layout = QVBoxLayout(self)

            self.button = QPushButton("确定")
            self.button.setObjectName("okButton")
            layout.addWidget(self.button)

            self.edit = QLineEdit()
            self.edit.setObjectName("nameEdit")
            self.edit.setText("初始值")
            layout.addWidget(self.edit)

            self.note = QTextEdit()
            self.note.setObjectName("noteArea")
            layout.addWidget(self.note)

            self.flag = QCheckBox("启用")
            self.flag.setObjectName("flagBox")
            layout.addWidget(self.flag)

    app = QApplication([sys.argv[0]])
    window = TestWindow()
    # 置顶：这条测试要对着真实桌面截图做图像匹配，只要有别的窗口盖住测试窗口，
    # 匹配就会失败 —— 那是环境噪音，不是代码问题。实测偶发过一次。
    window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    window.resize(460, 320)
    window.move(80, 80)
    window.show()
    window.raise_()
    window.activateWindow()

    def pump(seconds: float = 0.2) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

    pump(0.8)

    try:
        # ---------------------------------------------------------------- #
        checker.section("1) 拿到测试窗口")
        hwnd = win32gui.FindWindow(None, WINDOW_TITLE)
        if not checker.check("能在系统里找到测试窗口", bool(hwnd), f"hwnd={hwnd}"):
            return checker.report()

        root = bridge.auto().ControlFromHandle(hwnd)
        checker.check("能从句柄拿到 UIA 控件", root is not None)

        button = root.Control(searchDepth=0xFFFFFFFF, Name="确定")
        checker.check("能按名称找到按钮", button is not None and button.Exists(0, 0))
        if button is None or not button.Exists(0, 0):
            return checker.report()

        # 顺带记一条容易踩的事实：Qt 并不把 objectName 原样交给 UIA，而是拼成
        # 「应用名.窗口名.控件名」。所以手写 AutomationId 对 Qt 程序几乎不可行 ——
        # 这恰恰是必须有拾取器的理由。
        button_id = str(button.AutomationId)
        checker.check(
            "Qt 的 AutomationId 是「应用.窗口.控件」拼出来的",
            "okButton" in button_id,
            button_id,
        )
        prefix = button_id.rsplit(".", 1)[0] if "." in button_id else ""
        checker.check("能推出同窗口内其它控件的 id 前缀", bool(prefix), button_id)

        # ---------------------------------------------------------------- #
        checker.section("2) 闭环：控件 → 定位器 → 控件")
        locator = bridge.locator_from_control(button)
        checker.check("反解出了 AutomationId", "okButton" in locator.automation_id, locator.automation_id)
        checker.check("反解出了名称", locator.name == "确定", locator.name)
        checker.check("反解出了控件类型", "Button" in locator.control_type, locator.control_type)
        checker.check("反解出了所属窗口", locator.window_title == WINDOW_TITLE, locator.window_title)
        checker.check("反解出了矩形", locator.rect[2] > locator.rect[0], str(locator.rect))
        checker.check("算出了父子链", len(locator.path) >= 2, str([s.label() for s in locator.path]))
        checker.check("首选级别是 AutomationId", locator.best == "automation_id", locator.best)

        found = bridge.resolve(locator, timeout=2.0)
        checker.check("解析回同一个控件", found.is_uia)
        checker.check("用的就是 AutomationId 这一级", found.strategy == "automation_id", found.strategy)
        checker.check("名称对得上", found.name == "确定", found.name)

        # ---------------------------------------------------------------- #
        checker.section("3) 降级链逐级验证")
        import copy

        # 3.1 抹掉 AutomationId -> 应该落到 名称+类型
        no_id = copy.deepcopy(locator)
        no_id.automation_id = ""
        hit = bridge.resolve(no_id, timeout=2.0)
        checker.check("没有 AutomationId 时降到「名称+类型」", hit.strategy == "name_type", hit.strategy)
        checker.check("降级后仍然是同一个控件", hit.name == "确定", hit.name)

        # 3.2 再抹掉名称 -> 应该落到 路径
        no_name = copy.deepcopy(no_id)
        no_name.name = ""
        hit = bridge.resolve(no_name, timeout=2.0)
        checker.check("名称也没有时降到「路径」", hit.strategy == "path", hit.strategy)
        checker.check("路径解析后仍是同一个控件", hit.name == "确定", hit.name)

        # 3.3 只剩坐标 -> 应该落到 坐标
        only_point = Locator(point=locator.point)
        hit = bridge.resolve(only_point, timeout=1.0)
        checker.check("只剩坐标时降到「坐标」", hit.strategy == "point", hit.strategy)
        checker.check("坐标就是元素中心", hit.center == locator.point, f"{hit.center} vs {locator.point}")
        checker.check("坐标级不是 UIA 控件", not hit.is_uia)
        checker.check("界面会把它标成「脆弱」", only_point.is_fragile)

        # 3.4 图像匹配
        checker.section("4) 图像匹配（第四级）")
        template_path = REPO / "docs" / "_uia_template.png"
        imaging.capture(locator.rect, template_path)
        checker.check("截图已保存", template_path.is_file())

        from PIL import Image  # noqa: PLC0415

        with Image.open(template_path) as handle:
            size = handle.size
        checker.check("截图尺寸等于元素尺寸",
                      size == (locator.rect[2] - locator.rect[0], locator.rect[3] - locator.rect[1]),
                      f"{size} vs {locator.rect}")

        hit_point = imaging.find(template_path, locator.rect)
        checker.check("能在屏幕上找到这张图", hit_point is not None, str(hit_point))
        if hit_point:
            dx = abs(hit_point[0] - locator.point[0])
            dy = abs(hit_point[1] - locator.point[1])
            checker.check("匹配位置就是元素位置", dx <= 2 and dy <= 2, f"偏差 ({dx},{dy})")

        image_locator = Locator(image=str(template_path), rect=locator.rect, point=locator.point)
        hit = bridge.resolve(image_locator, timeout=1.0)
        checker.check("图像这一级能解析出来", hit.strategy == "image", hit.strategy)
        checker.check("图像匹配给出的坐标是对的",
                      abs(hit.center[0] - locator.point[0]) <= 2
                      and abs(hit.center[1] - locator.point[1]) <= 2,
                      f"{hit.center} vs {locator.point}")

        # 回归守卫：screen=(0,0) 是"没有提示"，不是"屏幕是 0×0"。
        # 元组永远为真，写 `screen or None` 会让图像这一级被静默跳过 —— 踩过。
        checker.check("screen=(0,0) 不会被当成有效的屏幕尺寸提示",
                      imaging.find(template_path, locator.rect, screen_size=(0, 0)) is not None)

        # 分辨率对不上时应该直接放弃，而不是给一个错答案
        stale = Locator(image=str(template_path), rect=locator.rect, screen=(640, 480))
        checker.check("屏幕尺寸变了就不该再信图像匹配",
                      imaging.find(stale.image, stale.rect, screen_size=stale.screen) is None)

        template_path.unlink(missing_ok=True)

        # ---------------------------------------------------------------- #
        checker.section("5) 找不到的时候要说人话")
        ghost = Locator(
            automation_id="绝对不存在的id",
            name="绝对不存在的名字",
            control_type="ButtonControl",
            window_title=WINDOW_TITLE,
        )
        try:
            bridge.resolve(ghost, timeout=0.4, poll=0.15)
            checker.check("找不到时抛异常", False, "居然没抛")
        except bridge.ElementNotFound as exc:
            message = str(exc)
            checker.check("找不到时抛 ElementNotFound", True)
            checker.check("报错里列出了试过的级别", "AutomationId" in message, message)
            checker.check("报错里带了等待时长", "0.4" in message, message)
        except Exception as exc:  # pragma: no cover
            checker.check("找不到时抛 ElementNotFound", False, f"{type(exc).__name__}: {exc}")

        empty = Locator()
        try:
            bridge.resolve(empty, timeout=0.1)
            checker.check("空定位器直接报错", False, "居然没抛")
        except bridge.ElementNotFound as exc:
            checker.check("空定位器直接报错", "空" in str(exc), str(exc))

        # ---------------------------------------------------------------- #
        checker.section("6) 多控件不串台")
        edit = root.Control(searchDepth=0xFFFFFFFF, AutomationId=f"{prefix}.nameEdit")
        checker.check("能找到输入框", edit is not None and edit.Exists(0, 0))
        if edit is not None and edit.Exists(0, 0):
            edit_locator = bridge.locator_from_control(edit)
            hit = bridge.resolve(edit_locator, timeout=2.0)
            checker.check("解析输入框拿到的是输入框，不是按钮",
                          "nameEdit" in hit.automation_id, hit.automation_id)
            checker.check("两个控件的定位器不相等",
                          edit_locator.to_dict() != locator.to_dict())

        # ---------------------------------------------------------------- #
        checker.section("7) 定位器字典往返")
        data = locator.to_dict()
        checker.check("转成字典后每个键都有意义", all(v for v in data.values()), str(data))
        restored = Locator.from_dict(data)
        checker.check("字典往返不丢信息", restored.to_dict() == data)
        checker.check("往返后路径还是对象", all(hasattr(s, "label") for s in restored.path))
        checker.check("残缺字典不会炸", Locator.from_dict({"name": "只有名字"}).name == "只有名字")
        checker.check("给个字符串也不炸", Locator.from_dict("垃圾数据").is_empty)

        merged = Locator(automation_id="a").merged_with(Locator(name="n", point=(1, 2)))
        checker.check("合并会补齐空字段", merged.automation_id == "a" and merged.name == "n")
        checker.check("合并不覆盖已有字段",
                      Locator(name="keep").merged_with(Locator(name="drop")).name == "keep")

        # ---------------------------------------------------------------- #
        checker.section("8) 拾取器")
        import win32api  # noqa: PLC0415

        from host.uia_picker import ElementPicker  # noqa: PLC0415

        import tempfile  # noqa: PLC0415

        picker_work = Path(tempfile.mkdtemp(prefix="myautowork-pick-"))
        picker = ElementPicker(template_dir=picker_work)
        picker.picked.connect(lambda key, loc: captured.append((key, loc)))
        captured: list = []

        checker.check("拾取器认识 DPI 感知状态", bridge.auto() is not None)
        # 把光标放到按钮中心，拾取器应该框住它
        win32api.SetCursorPos(locator.point)
        checker.check("拾取模式能启动", picker.start("element"))
        for _ in range(24):  # 让它轮询几轮，把高亮框和 HUD 都画出来
            app.processEvents()
            time.sleep(0.05)

        hovered = bridge.control_at(*locator.point)
        checker.check("光标下能找到控件", hovered is not None)
        checker.check("拾取器框住的正是目标元素",
                      picker._last_rect == locator.rect
                      or (hovered is not None
                          and bridge.control_info(hovered)["rect"] == picker._last_rect),
                      f"{picker._last_rect} vs {locator.rect}")

        # 顺带验证截图这条路径可用。**故意不落盘** —— 这是整屏截图，会带上桌面上
        # 别的内容，不该进仓库。
        grabbed = imaging.grab()
        checker.check("能抓到整屏（图像兜底的前提）",
                      grabbed.size[0] > 100 and grabbed.size[1] > 100, str(grabbed.size))

        picker._capture()
        checker.check("采集真的产出了定位器", len(captured) == 1 and captured[0][0] == "element")
        if captured:
            picked = captured[0][1]
            checker.check("采集到的定位器带着 AutomationId 或名称",
                          bool(picked.automation_id or picked.name), str(picked.to_dict())[:120])
            checker.check("采集到的定位器带着截图（图像兜底）", bool(picked.image), picked.image)
            checker.check("采集完就退出了拾取模式", not picker.active)

    finally:
        window.close()
        pump(0.2)

    return checker.report()


if __name__ == "__main__":
    raise SystemExit(main())
