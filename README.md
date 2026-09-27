# MyAutoWork

以插件为核心的 Windows 自动化工具。

![我的流程](docs/screenshot-home.png)

---

## 为什么做这个

把自动化能力拆成一个个小模块。每个模块只负责一件事 —— 控制某个软件、跑一段固定脚本、或者让 AI 做个判断。想要什么就装什么，不想要就拆掉。

内核只把这些模块连起来，保证它们能配合、别乱来。它不认识任何一个具体模块，所以加新能力不用动它。

于是整个工具不是一块写死的整体，而是能像搭积木一样。

---

## 快速开始

需要 **Windows** 和 **Python 3.10+**（[下载](https://www.python.org/downloads/)，安装时记得勾上 *Add Python to PATH*）。

```bat
git clone https://github.com/Altriazyk/MyAutoWork.git
cd MyAutoWork
py -m venv .venv
.venv\Scripts\pip install -e ".[ui]"
.venv\Scripts\python -m host.app
```

第一次会下载 PySide6（几十兆）。


## 深入

设计上的取舍、架构、怎么自己写插件，都在 [`docs/DESIGN.md`](docs/DESIGN.md)。

## 许可

[MIT](LICENSE)
