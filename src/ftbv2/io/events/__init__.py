"""事件提取的 IO 层。当前只有密度回归：按注册表的不变量在样本日上切出候选，
给出分布与实测密度——切割规则要由数据回归出来，不由我们指定（2026-09-03 用户裁定）。
判据与适用时段从 `core.registry` 取，不在这里重写一份。
公开接口 = 本文件的 __all__（architecture.toml：io.events）；跨模块只能从这里 import。"""

from ftbv2.io.events.probe import HIDDEN, REFILL, WALLS, Candidates, DayProbe, Probe, probe

__all__ = ["HIDDEN", "REFILL", "WALLS", "Candidates", "DayProbe", "Probe", "probe"]
