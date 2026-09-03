"""事件注册表：全部事件定义的唯一来源。纯逻辑核，不触碰 IO。
公开接口 = 本文件的 __all__（architecture.toml：core.registry）；跨模块只能从这里 import。

条目声明的是**要什么**（事件型 · 回看范围 · stream · 几何度量 · 切割规则 · 提取参数），
不是**怎么跑**——怎么跑由驱动层（io.events）照着声明决定。"""

from ftbv2.core.registry.registry import (
    REGISTRY_VERSION,
    day_boundary,
    digest,
    extraction_params,
    kinds,
    spec,
    version,
)
from ftbv2.core.registry.seeds import (
    DAY_BOUNDARY,
    QUOTE_THEN_WITHDRAW,
    SEEDS,
    SEQ_REPEATED_SAME_PX_VOL,
    SNAPSHOT_REST_GEQ_DISP,
    VOL_CLOCK_BAR,
)
from ftbv2.core.registry.types import (
    Contamination,
    DayBoundarySpec,
    EventSpec,
    Lookback,
    Measure,
    Param,
    ParamRole,
    Shape,
)

__all__ = [
    "DAY_BOUNDARY", "QUOTE_THEN_WITHDRAW", "REGISTRY_VERSION", "SEEDS", "SEQ_REPEATED_SAME_PX_VOL",
    "SNAPSHOT_REST_GEQ_DISP", "VOL_CLOCK_BAR", "Contamination", "DayBoundarySpec", "EventSpec",
    "Lookback", "Measure", "Param", "ParamRole", "Shape", "day_boundary", "digest",
    "extraction_params", "kinds", "spec", "version",
]
