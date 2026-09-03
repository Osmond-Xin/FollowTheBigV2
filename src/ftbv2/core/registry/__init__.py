"""事件注册表：全部事件定义的唯一来源。纯逻辑核，不触碰 IO。
公开接口 = 本文件的 __all__（architecture.toml：core.registry）；跨模块只能从这里 import。

条目声明的是**要什么**（事件型 · 回看范围 · stream · 适用时段 · 排序键 · 几何度量 ·
不变量 · 切割规则 · 提取参数 · 密度目标），不是**怎么跑**——怎么跑由驱动层（io.events）
照着声明决定。**实测的数字不在这里**：它住在收据里，由 `admit_full_extraction()` 比对。"""

from ftbv2.core.registry.predicates import (
    InvariantCode,
    holds,
    predicate,
    required_fields,
)
from ftbv2.core.registry.registry import (
    REGISTRY_VERSION,
    admit_full_extraction,
    candidate_variables,
    day_boundary,
    digest,
    extraction_params,
    kinds,
    spec,
    structural_events,
    uncontrasted,
    unmeasured,
    version,
)
from ftbv2.core.registry.seeds import (
    DAY_BOUNDARY,
    FILL_EXCEEDS_DISPLAYED,
    LEVEL_BUILD_THEN_VANISH,
    REFILL_AFTER_FILL,
    SEEDS,
    TOTAL_ORDER,
    VOL_CLOCK_BAR,
)
from ftbv2.core.registry.types import (
    BarTermination,
    Contamination,
    CoverageStatus,
    DayBoundarySpec,
    DensityMeasurement,
    DensityTarget,
    EventClass,
    EventSpec,
    EvidenceRef,
    GroupCloseReason,
    Lookback,
    Measure,
    MeasureRole,
    Param,
    ParamRole,
    Relation,
    Shape,
    Side,
    yields_events,
)

__all__ = [
    "DAY_BOUNDARY", "FILL_EXCEEDS_DISPLAYED", "LEVEL_BUILD_THEN_VANISH", "REFILL_AFTER_FILL",
    "REGISTRY_VERSION", "SEEDS", "TOTAL_ORDER", "VOL_CLOCK_BAR", "BarTermination", "Contamination",
    "CoverageStatus", "DayBoundarySpec", "DensityMeasurement", "DensityTarget", "EventClass", "EventSpec",
    "EvidenceRef", "GroupCloseReason", "InvariantCode", "Lookback", "Measure", "MeasureRole", "Param",
    "ParamRole", "Relation", "Shape", "Side", "admit_full_extraction", "candidate_variables", "day_boundary",
    "digest", "extraction_params", "holds", "kinds", "predicate", "required_fields", "spec",
    "structural_events", "uncontrasted", "unmeasured", "version", "yields_events",
]
