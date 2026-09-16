"""轮次核验结论词表（工单 0025 自洽性核验；工单 0026 起接入迭代日志入账与渲染）。

routers/projects.py（判定产出、卡片与收尾事件字段）与 agent/prompts.py（迭代日志
的模型侧渲染文案）共用本词表，消除同一枚举值在两处各写一遍字面量的漂移风险；
0028 正确性裁判引入越界（out_of_scope）/未完成（incomplete）等结论时在此扩展。

测试房规：HTTP 接缝的断言继续用字面量钉住对外契约，不 import 本模块——期望值
必须来自独立的真相来源，否则退化为同义反复（规格 0021 Testing Decisions）。
"""

# 自洽性结论（卡片 consistency / 收尾事件 verdict / 迭代日志 verdict 字段同源）
CONSISTENT = "consistent"
MISMATCH = "mismatch"
FALLBACK = "fallback"  # 模型始终未走唯一出口：产物由系统按磁盘事实兜底，无申报可比

# 失配种类（MISMATCH 的细分，卡片与迭代日志的 mismatch_kind 字段同源）
VERBAL_COMPLETION = "verbal_completion"
UNDECLARED_CHANGE = "undeclared_change"
FILE_SET_MISMATCH = "file_set_mismatch"

# 正确性裁决结论（工单 0028 / ADR 0005「第 8 层」）：卡片与迭代日志的
# scope_verdict 字段同源。前三值是裁判的合法输出枚举；UNVERIFIED 不是模型
# 裁决值，而是裁判失败（网络/超时/非法输出，重试耗尽）时系统标注的
# 「核验未完成」——安全网故障必须显式化而非静默吞掉，且快照照建。
IN_SCOPE = "in_scope"
OUT_OF_SCOPE = "out_of_scope"
INCOMPLETE = "incomplete"
UNVERIFIED = "unverified"
SCOPE_VERDICT_ENUM = (IN_SCOPE, OUT_OF_SCOPE, INCOMPLETE)
