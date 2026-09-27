"""myautowork 图形界面。

界面是内核的**消费者**，不是内核的一部分。它读的是同一份插件接口描述、同一份工作流
JSON、同一套运行事件：

    首页流程列表  ← kernel.library.WorkflowLibrary   有哪些自动化，上次跑得怎么样
    模块面板      ← registry.catalog()               插件声明了什么能力
    属性面板      ← node_spec()["inputs"]            参数类型 → 自动生成控件
    画布          ← kernel.graph.Workflow            双线模型
    运行态        ← Engine(on_event=...)             节点开始/完成/失败/日志

所以界面里不该出现任何"插件专有"的代码。加一个插件，界面一行都不用改。

界面分两页：**首页是流程列表**（点击就跑），编辑器是二级页面（点"编辑"才进）。
"""

from __future__ import annotations

__version__ = "0.1.0"
