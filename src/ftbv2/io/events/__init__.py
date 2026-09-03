"""事件提取的 IO 层。当前只有假墙密度回归（`LevelBuildThenVanish` 候选生成 + 分布）——
切割规则的定义要由数据回归出来，不由我们指定（2026-09-03 用户裁定）。
公开接口 = 本文件的 __all__（architecture.toml：io.events）；跨模块只能从这里 import。"""

from ftbv2.io.events.probe import WallProbe, probe_walls

__all__ = ["WallProbe", "probe_walls"]
