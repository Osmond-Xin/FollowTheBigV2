"""事件注册表的契约测试。CONTEXT.md 第三节的每一条裁定，在这里都必须有一个会红的测试——
定义写在文档里而门禁查不到，等于没写。

对照表（裁定 → 测试）：
- 事件三型 → test_每条声明了事件型
- 回看范围四档 · 跨日型写得出常数 → test_跨日型必须写得出天数 / test_非跨日型不得带跨日字段
- 周期型不产出事件行 → test_周期型不得成为事件条目
- 边界由结构触发不由时间触发 → test_每条都有开段与结段规则 / test_结段规则不得由时间阈值触发
- 提取参数只能是拓扑量 → test_判断阈值不得进注册表
- 每类事件一张表 → test_类型名唯一且是合法表名
- 污染级别是必填参考值 → test_每条声明了污染级别
- 每类提取器至少一条 → test_第一批覆盖每一类提取器
"""

import pytest

from ftbv2.core.raw import STREAMS
from ftbv2.core.registry import (
    SEEDS,
    Contamination,
    EventSpec,
    Lookback,
    Measure,
    Param,
    ParamRole,
    Shape,
    day_boundary,
    digest,
    extraction_params,
    kinds,
    spec,
    version,
)

REGISTRY_DIGEST = "3d82bd11e53a2824"
"""金标准摘要。改任何一条条目都会让它变——**改摘要必须同时改 REGISTRY_VERSION**：
改切割算法或参数 = major（全量重跑，隔离旧流）；新增独立事件类型 = minor（增量追加）；
纯重构、产物哈希不变 = patch。这条测试的全部意义就是不让定义被静默改动。"""


def _seed(**over: object) -> EventSpec:
    """一条合法的最小条目，测试按需覆盖单个字段。"""
    base = {
        "kind": "Probe",
        "alias": "探针",
        "shape": Shape.INTERVAL,
        "lookback": Lookback.INTRA_EPISODE,
        "streams": ("orders",),
        "open_rule": "出现一笔委托",
        "close_rule": "该笔委托被撤销",
        "measures": (Measure("vol", "int64", "股", "委托量"),),
        "params": (),
        "contamination": Contamination.UNAWARE,
        "v1_audit": "V1 未做过此结构",
        "max_rows_per_symbol_day": 100,
        "expected_rows_per_symbol_day": 10,
    }
    return EventSpec(**(base | over))          # type: ignore[arg-type]


# ---------------------------------------------------------------- 版本与摘要

def test_摘要没有被静默改动() -> None:
    assert digest() == REGISTRY_DIGEST, (
        "注册表条目变了。这不是坏事，但必须是有意的：同时更新 REGISTRY_VERSION 与本测试的金标准。"
        f"当前 {digest()}，金标准 {REGISTRY_DIGEST}"
    )


def test_版本号在接口上() -> None:
    assert version() == "0.1.0"


# ---------------------------------------------------------------- 事件三型

def test_每条声明了事件型() -> None:
    for s in SEEDS:
        assert isinstance(s.shape, Shape), s.kind


def test_时刻型的语义写进了结段规则() -> None:
    """时刻型 start_time == end_time；因子层不得把三型的 end_time 一概当作「事情结束了」。"""
    for s in SEEDS:
        if s.shape is Shape.INSTANT:
            assert "start_time == end_time" in s.close_rule, s.kind


# ---------------------------------------------------------------- 回看范围四档

def test_周期型不得成为事件条目() -> None:
    """周期尺度的量落日级参考层的列——处境不是事件。"""
    with pytest.raises(ValueError, match="周期型不产出事件行"):
        _seed(lookback=Lookback.CYCLE)


def test_跨日型必须写得出天数() -> None:
    """分界是能不能事先写下一个常数：写不出一个数的不是跨日型。"""
    with pytest.raises(ValueError, match="写不出一个数的不是跨日型"):
        _seed(lookback=Lookback.CROSS_DAY, daily_ref_columns=("volume",))


def test_跨日型必须声明读哪些日级参考层列() -> None:
    with pytest.raises(ValueError, match="没声明读哪些日级参考层列"):
        _seed(lookback=Lookback.CROSS_DAY, lookback_days=20)


def test_非跨日型不得带跨日字段() -> None:
    with pytest.raises(ValueError, match="只有跨日型读 T−1"):
        _seed(lookback=Lookback.INTRA_DAY, lookback_days=5)
    with pytest.raises(ValueError, match="只有跨日型读 T−1"):
        _seed(lookback=Lookback.INTRA_EPISODE, daily_ref_columns=("volume",))


def test_刻度不得取自当日自身() -> None:
    """否则 09:35 的 bar 边界取决于 14:55，事件流的存在性本身就携带未来信息。"""
    for s in SEEDS:
        if s.lookback is Lookback.CROSS_DAY:
            assert s.lookback_days and s.lookback_days > 0, s.kind
            why = " ".join(p.why for p in s.params)
            assert "绝不含当日自身" in why, f"{s.kind} 没写明刻度不取自当日"


# ---------------------------------------------------------------- 边界由结构触发

def test_每条都有开段与结段规则() -> None:
    with pytest.raises(ValueError, match="边界由结构触发不由时间触发"):
        _seed(close_rule="")
    with pytest.raises(ValueError, match="边界由结构触发不由时间触发"):
        _seed(open_rule="")


def test_结段规则由结构事件触发而不是时钟() -> None:
    """庄家不掐表，是子弹打光了或者量到了。结段的触发只能是本方行为的结构变化。
    这条靠人读——但至少保证每条结段规则里出现了一个结构触发词，纯时间词不算。"""
    结构触发词 = ("撤销", "成交", "委托", "反转", "跨越", "价", "量", "时刻")
    for s in SEEDS:
        assert any(w in s.close_rule for w in 结构触发词), f"{s.kind} 的结段规则看不出结构触发：{s.close_rule}"


# ---------------------------------------------------------------- 提取参数

def test_判断阈值不得进注册表() -> None:
    with pytest.raises(ValueError, match="判断阈值属于预注册"):
        Param("theta", ParamRole.DECISION_THRESHOLD, 0.9, "分位", "想混进来")


def test_召回闸必须登记召回覆盖率实测() -> None:
    with pytest.raises(ValueError, match="未登记召回覆盖率实测"):
        Param("gate", ParamRole.CANDIDATE_RECALL_GATE, 3, "笔", "调紧单调减少候选")


def test_提取参数必须说清为什么不是判断() -> None:
    with pytest.raises(ValueError, match="为什么它是拓扑量而不是判断"):
        Param("k", ParamRole.STRUCTURAL_PARTITION, 2, "笔", "")


def test_全部提取参数可枚举进证据指纹() -> None:
    params = extraction_params()
    assert set(params) == set(kinds())
    for kind, ps in params.items():
        for p in ps:
            assert p.role is not ParamRole.DECISION_THRESHOLD, kind


# ---------------------------------------------------------------- 每类一张表

def test_类型名唯一且是合法表名() -> None:
    ks = kinds()
    assert len(set(ks)) == len(ks)
    for k in ks:
        assert k.isidentifier(), k


def test_未登记的类型查不到而不是返回空() -> None:
    """「查不到 = 没有」被禁止。"""
    with pytest.raises(KeyError, match="未登记的事件类型"):
        spec("不存在的事件")


def test_几何度量不得重名() -> None:
    m = Measure("vol", "int64", "股", "委托量")
    with pytest.raises(ValueError, match="几何度量重名"):
        _seed(measures=(m, m))


# ---------------------------------------------------------------- 条目必填

def test_每条声明了污染级别() -> None:
    """是参考值不是判据：必填、进证据指纹、可被审计，但不进入任何机械判定。"""
    for s in SEEDS:
        assert isinstance(s.contamination, Contamination), s.kind


def test_第一批种子的出处都是V1失效模式审计() -> None:
    for s in SEEDS:
        assert s.contamination is Contamination.KNOWS_VERDICT, s.kind
        assert s.v1_audit, s.kind


def test_缺V1审计不得进注册表() -> None:
    with pytest.raises(ValueError, match="V1 失效模式审计"):
        _seed(v1_audit="")


def test_必须声明期望条数且不超预算() -> None:
    """信号稀少是设计目标：条目要声明期望条数，超预算硬失败、不发布。"""
    with pytest.raises(ValueError, match="必须声明期望条数"):
        _seed(expected_rows_per_symbol_day=101)
    with pytest.raises(ValueError, match="必须声明期望条数"):
        _seed(expected_rows_per_symbol_day=0)


def test_stream必须已登记() -> None:
    with pytest.raises(ValueError, match="含未登记流"):
        _seed(streams=("level3",))
    with pytest.raises(ValueError, match="为空或含未登记流"):
        _seed(streams=())


def test_结构名之外必须另列语义别名() -> None:
    """event_type == "spoof" 这一列本身就是对下游的语义污染。"""
    with pytest.raises(ValueError, match="语义别名单列"):
        _seed(alias="")


# ---------------------------------------------------------------- 第一批的选择标准

def test_第一批覆盖每一类提取器() -> None:
    """选择标准不是「便宜」，是每类提取器至少一条——只做段内型，第二批一来跨日型就得改接口。"""
    assert {s.shape for s in SEEDS} == {Shape.INTERVAL, Shape.GROUP, Shape.INSTANT}
    assert {s.lookback for s in SEEDS} == {
        Lookback.INTRA_EPISODE, Lookback.INTRA_DAY, Lookback.CROSS_DAY,
    }
    assert any(len(s.streams) > 1 for s in SEEDS), "缺多流对齐的条目，驱动层的对齐能力没被验证"
    assert all(s.streams[0] in STREAMS for s in SEEDS)


# ---------------------------------------------------------------- 日界事件

def test_日界事件不是注册表条目() -> None:
    """它由驱动层从 交易日历 × 标的主表 × 摄取收据的叉积产生。"""
    assert "DayBoundary" not in kinds()
    assert day_boundary().kind == "DayBoundary"


def test_日界事件带覆盖状态与归因码() -> None:
    """安静（有数据、无结构）与缺口（数据不存在）是两种信息，不得合并。"""
    names = {m.name for m in day_boundary().measures}
    assert {"coverage_status", "gap_codes"} <= names


def test_日界事件承载window_stats() -> None:
    """因子层直接读，不必自己扫全天。V1 唯一存活几何「持续安静」的原料在这里。"""
    names = {m.name for m in day_boundary().measures}
    assert {"n_events", "quiet_span_ms"} <= names
