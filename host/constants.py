"""画布相关的共享常量。"""

from __future__ import annotations

#: 从模块面板拖到画布时用的 MIME 类型，内容是 "plugin\naction"。
NODE_MIME = "application/x-myautowork-node"

#: 方向
DIR_IN = "in"
DIR_OUT = "out"

#: 端口类别
KIND_EXEC = "exec"
KIND_DATA = "data"

#: 执行入口端口的名字。工作流 JSON 里执行边不记录 ``to_port``（引擎不关心），
#: 所以从文件还原连线时必须知道画布上这个端口叫什么。
EXEC_IN_PORT = "in"

#: 执行出口
PORT_SUCCESS = "success"
PORT_ERROR = "error"

#: 首页的两种展示方式。切换入口在顶部「视图」菜单里。
VIEW_CARD = "card"  # 网格：固定尺寸方砖，摆不下换行
VIEW_LIST = "list"  # 列表：一行一条，占满宽度

__all__ = [
    "NODE_MIME",
    "DIR_IN",
    "DIR_OUT",
    "KIND_EXEC",
    "KIND_DATA",
    "EXEC_IN_PORT",
    "PORT_SUCCESS",
    "PORT_ERROR",
    "VIEW_CARD",
    "VIEW_LIST",
]
