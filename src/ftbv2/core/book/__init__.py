"""盘口重建：从逐笔流算出档位深度时间线与档位生命周期，从十档快照算出帧间的档位变化。
纯逻辑核，不触碰 IO。
公开接口 = 本文件的 __all__（architecture.toml：core.book）；跨模块只能从这里 import。"""

from ftbv2.core.book.depth import (
    TICK,
    DeltaResult,
    attach_touch,
    attach_visibility,
    depth_deltas,
    level_episodes,
    quote_levels,
)
from ftbv2.core.book.frames import frame_levels, frame_transitions

__all__ = ["TICK", "DeltaResult", "attach_touch", "attach_visibility", "depth_deltas",
           "frame_levels", "frame_transitions", "level_episodes", "quote_levels"]
