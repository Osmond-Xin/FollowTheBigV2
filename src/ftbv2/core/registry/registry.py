"""注册表的查询入口与版本。事件提取与因子都只从这里取定义，不得各自持有一份。

版本规则（CONTEXT.md「事件流版本」）：改切割算法或参数 = major（全量重跑，隔离旧流）；
新增独立事件类型 = minor（增量追加）；纯重构、产物哈希不变 = patch。
`digest()` 是全部条目的内容摘要——改了任何一条，摘要就变；契约测试用金标准摘要盯住它，
逼得每一次改动都必须同时动 REGISTRY_VERSION，改不动版本就改不了定义。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from enum import Enum

from ftbv2.core.registry.seeds import DAY_BOUNDARY, SEEDS
from ftbv2.core.registry.types import (
    DayBoundarySpec,
    DensityMeasurement,
    EventClass,
    EventSpec,
    Param,
)

REGISTRY_ROW_BUDGET = 100.0
"""整张注册表合计的条数上界，条/(标的·日)。**这才是真正的约束**，单条的上界只防独吞。

怎么来的（2026-09-03，用户裁定「动上界」之后重新推导，取代原来那个没有出处的 10–30）：

    事件层常驻预算 8 GB ÷ 每行约 25 字节 ≈ 3.44 亿行
    ÷ (约 3000 主板标的 × 1122 个交易日) ≈ 102 条/(标的·日)

⚠️ **两个输入都是估算，且它们的性质不同**：
- 8 GB 来自数据表第五节，那里已注明「这个数字是估算，尚未实测」；
- 每行 25 字节是按九列定点整数 + 枚举、parquet 压缩后的估算，**没有实测过**。

所以 100 这个数**是一个预算分配决定，不是一个测量结果**。它可以被改，
但改它要改的是预算或者字节数的估算——那时该拿出实测，而不是因为某条条目没过就调它。
改动会改变 `digest()`，必须同时改 `REGISTRY_VERSION`。
"""

REGISTRY_VERSION = "0.6.0"
"""注册表版本。

0.2.0：2026-09-03 审计后重写切割规则（假墙与冰山由单行过滤改为跨行结构关系）。
0.3.0：三方红队处置 + 假墙可见性实测落地——不变量由自由字符串改为可执行谓词码；
实测密度移出条目（纯核只留目标）；新增 event_class / windows / total_order /
contrast_verdict_ref；假墙加「峰值时刻十档内可见」；成交量时钟刻度由成交量改成交额。
0.4.0：密度上界重新推导。原来的「10–30 条/(标的·日)」没有出处，红队方法论致命 1 判为
「未经预注册却当机械门禁的验收基线」；隐藏深度实测 32.68 撞上它之后，用户裁定动上界。
新的口径是**整张表的合计**（`REGISTRY_ROW_BUDGET`，由常驻预算 ÷ 每行字节推导），
单条上界降级为「防独吞」的护栏。
0.5.0：冰山加「每片超过一手」。实测 218.92 条/(标的·日) 三关全不过，
每片量中位在每一个桶里都是 100 股；一手是交易所定的最小委托单位，
**一手不能再切，所以一手的重复挂单不是切片的证据**（2026-09-03 用户裁定）。属改切割规则。
0.6.0：冰山**换了机制**。用户指出「这么多条说明计算方法有问题，肯定把常见情况也算成冰山了」——
是对的：逐笔数据里没有账户身份，上一版拿「同价 + 同量」当身份，数的是
「同一价位上碰巧同量的委托，其中一笔被吃过」。改为**档位深度的循环**
（被一笔委托堆起来 → 被成交整个吃光 → 回到零 → 同幅度再来），
用「档位归零」替代身份：中间没有别人，补上来的只能是同一个人。
改切割算法 = major，0.x 阶段用 minor 位表达，第一片全量前不锁 1.0。
"""

_BY_KIND: dict[str, EventSpec] = {s.kind: s for s in SEEDS}
if len(_BY_KIND) != len(SEEDS):
    raise ValueError("注册表有重名条目：kind 是事件流分表的表名，必须唯一")


def kinds() -> tuple[str, ...]:
    """全部事件类型名，按登记顺序。**每类事件各自一张表**——这也是落盘的表名。"""
    return tuple(_BY_KIND)


def spec(kind: str) -> EventSpec:
    """取一条条目。未登记的 kind 抛 KeyError 并列出已登记的——「查不到 = 没有」被禁止。"""
    try:
        return _BY_KIND[kind]
    except KeyError:
        raise KeyError(f"未登记的事件类型 {kind!r}；已登记：{', '.join(_BY_KIND)}") from None


def day_boundary() -> DayBoundarySpec:
    """日界事件的 schema。不是注册表条目（由驱动层产生），放在这里只为事实单源。"""
    return DAY_BOUNDARY


def structural_events() -> tuple[str, ...]:
    """适用密度目标的条目。尺子（bar）与参考层不在其中——它们的条数是设计出来的。"""
    return tuple(k for k, s in _BY_KIND.items() if s.event_class is EventClass.STRUCTURAL_EVENT)


def unmeasured(measurements: Mapping[str, DensityMeasurement]) -> tuple[str, ...]:
    """还没在真实数据上测过密度的结构事件。

    **参数不是可选的**：实测住在收据里，不在源码里（红队 2026-09-03 架构严重 5）。
    上一版这个函数从 `spec.density is None` 读答案，那要求有人把量出来的数字反向写回源码。
    现在调用方必须先把收据读进来，说得出自己拿的是哪一批实测。
    """
    return tuple(k for k in structural_events() if k not in measurements)


def admit_full_extraction(kind: str, measurement: DensityMeasurement | None) -> DensityMeasurement:
    """全量提取前的准入：这条实测够不够格让驱动层花掉 15 小时。

    三关，任一不过即抛：**测过** · **不超成本上界** · **确实降了维**。
    「预算是拍的」必须在花掉 15 小时之前暴露，而不是之后。

    ⚠️ 实测落在目标之外时，该做的是**回到设计**，不是把目标调成能过——
    目标写在条目里、进 `digest()`，改它必须同时改 `REGISTRY_VERSION`（红队 2026-09-03 方法论严重 4）。
    """
    s = spec(kind)
    if s.event_class is not EventClass.STRUCTURAL_EVENT:
        raise ValueError(f"{kind} 是 {s.event_class.value}，不走密度准入：它的条数由采样分辨率决定")
    target = s.density_target
    assert target is not None, "结构事件必有密度目标，由 EventSpec 构造时保证"
    if measurement is None:
        raise ValueError(
            f"{kind} 还没在真实数据上测过密度。先在样本日上跑一趟拿到条数与坍缩比、带收据，再进全量提取"
        )
    if measurement.kind != kind:
        raise ValueError(f"实测记的是 {measurement.kind}，要准入的是 {kind}：拿错了收据")
    if measurement.event_rows == 0:
        raise ValueError(
            f"{kind} 在 {measurement.symbol_days} 个标的·日上一条都没切出来。"
            "**零条不是「密度很低」，是「这一趟什么都没量到」**：要么样本不对，要么这条结构不成立。"
            "坍缩比在这里是无穷大，放它过去就等于用一个除零的数字批准 15 小时"
        )
    if measurement.rows_per_symbol_day > target.max_rows_per_symbol_day:
        raise ValueError(
            f"{kind} 实测 {measurement.rows_per_symbol_day:.2f} 条/(标的·日) 超过上界 "
            f"{target.max_rows_per_symbol_day}：这条要么切得太宽，要么它根本不稀有。"
            "回到设计，不要改上界"
        )
    if measurement.collapse_ratio < target.min_collapse_ratio:
        raise ValueError(
            f"{kind} 实测坍缩比 {measurement.collapse_ratio:.0f} 低于下界 {target.min_collapse_ratio}："
            "降维不足，事件流只是原始层的另一种排列"
        )
    return measurement


def admit_registry(measurements: Mapping[str, DensityMeasurement]) -> float:
    """整张表的合计准入：全部结构事件加起来占不占得下常驻预算。返回合计条数。

    **这才是真正的成本约束。** 单条的上界只防一条吃掉半个预算；
    一条条目占多少合适，要看别的条目占了多少——预算是共用的。

    未实测的条目让本函数拒绝：**不知道就是不知道**，不许按零计入合计蒙混过关。
    """
    missing = unmeasured(measurements)
    if missing:
        raise ValueError(
            f"还有条目没测过密度：{', '.join(missing)}。合计预算算不出来——"
            "把没测的按零计入，等于假装它们不占地方"
        )
    total = sum(measurements[k].rows_per_symbol_day for k in structural_events())
    if total > REGISTRY_ROW_BUDGET:
        raise ValueError(
            f"全部结构事件合计 {total:.2f} 条/(标的·日) 超过预算 {REGISTRY_ROW_BUDGET}："
            "事件层装不下。要么收窄某条结构，要么拿出实测重新推导预算——"
            "**不要因为某一条没过就调预算**"
        )
    return total


def uncontrasted() -> tuple[str, ...]:
    """还没安排对照裁决的条目。与密度同级的一道门禁（红队 2026-09-03 方法论致命 3）。"""
    return tuple(k for k, s in _BY_KIND.items() if not s.contrast_verdict_ref)


def candidate_variables() -> dict[str, tuple[str, ...]]:
    """每条条目留给因子层的备选变量。

    **这些名字的总数就是多重检验作用域的下界**：每一个都可能变成一个因子假设，
    每一个假设都要消耗 episode 预算。把它数出来，是为了让组合空间在预注册之前就是可见的、
    可争论的，而不是等到有人一口气提了三十个因子才发现（红队 2026-09-03 方法论严重 5）。
    """
    return {k: s.candidate_variables() for k, s in _BY_KIND.items()}


def extraction_params() -> dict[str, tuple[Param, ...]]:
    """全部提取参数，按事件类型分组。进证据指纹：假设不得修改它，每个假设必须声明所依赖的事件流版本。"""
    return {s.kind: s.params for s in SEEDS}


def version() -> str:
    """注册表版本号。事件流版本 = 它 + 提取参数集 + 原始层数据清单摘要。"""
    return REGISTRY_VERSION


def digest() -> str:
    """全部条目（含日界事件 schema）的内容摘要，sha256 前 16 位。
    条目的任何改动都会改变它——契约测试用金标准盯住，改定义必须同时改版本。"""
    payload = {
        "version": REGISTRY_VERSION,
        "events": [_canonical(s) for s in SEEDS],
        "day_boundary": _canonical(DAY_BOUNDARY),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _canonical(obj: EventSpec | DayBoundarySpec) -> dict[str, object]:
    return asdict(obj, dict_factory=lambda kv: {k: _plain(v) for k, v in kv})


def _plain(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, type) and issubclass(value, Enum):      # Measure.enum_type：摘要绑住枚举的取值
        return {"enum": value.__name__, "values": [m.value for m in value]}
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    return value
