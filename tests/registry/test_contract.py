"""事件注册表的契约测试。

**只写会红的测试。** 2026-09-03 审计删掉了上一版里的三类门面：
- 重言式：`assert isinstance(s.shape, Shape)`——字段类型就是 Shape，测不出任何东西；
- 字符串在场检查：查 `why` 里有没有「绝不含当日自身」这句话，是查我打了字，不是查行为；
- 关键词匹配：「结构触发词」表里有「价」「量」「时刻」，任何谈委托的中文都命中，
  既放得过一切也拦不住一切。
留着比没有更坏：它们让「28 个契约测试」这句话变成谎话。

对照表（裁定 → 测试）：
- 单行过滤不是提取        → test_结构关系至少两个角色 / test_坍缩比不得小于等于一
- 密度必须实测不得估算    → test_未实测的条目拒绝进入全量提取 / test_第一批还没测过密度
- 周期型不产出事件行      → test_周期型不得成为事件条目
- 跨日型写得出常数        → test_跨日型必须写得出天数 / test_非跨日型不得带跨日字段
- 提取参数只能是拓扑量    → test_判断阈值不得进注册表
- 提取参数必须可执行      → test_参数值必须是数而不是一句描述
- 枚举必须单源            → test_枚举取值必须有类型 / test_日界事件的归因码复用缺陷账本
- 每类一张表              → test_类型名唯一且是合法表名
- 定义不得被静默改动      → test_摘要没有被静默改动
"""

import pytest

from ftbv2.core.raw import STREAMS, DefectCode
from ftbv2.core.registry import (
    SEEDS,
    Contamination,
    CoverageStatus,
    Density,
    EventSpec,
    Lookback,
    Measure,
    Param,
    ParamRole,
    Relation,
    Shape,
    Side,
    day_boundary,
    digest,
    extraction_params,
    kinds,
    spec,
    unmeasured,
    version,
)

REGISTRY_DIGEST = "dc70ee89a1919206"
"""金标准摘要（含每个 enum_type 的取值集合）。改任何一条条目都会让它变——
**改摘要必须同时改 REGISTRY_VERSION**。这条是本文件里最有用的一个测试：
它不判断对错，只保证没有人能悄悄改掉定义。"""

_REL = Relation(roles=("甲", "乙"), invariant="甲在乙之前")


def _seed(**over: object) -> EventSpec:
    base = {
        "kind": "Probe", "alias": "探针", "shape": Shape.INTERVAL, "lookback": Lookback.INTRA_EPISODE,
        "streams": ("orders",), "relation": _REL, "open_rule": "甲出现", "close_rule": "乙出现",
        "measures": (Measure("vol", "int", "股", "委托量"),), "params": (),
        "contamination": Contamination.UNAWARE, "v1_audit": "V1 未做过此结构",
    }
    return EventSpec(**(base | over))          # type: ignore[arg-type]


# ------------------------------------------------- 单行过滤不是提取（本次审计的核心）

def test_结构关系至少两个角色() -> None:
    """一条事件必须由多条原始记录的关系构成。单行属性判断是过滤，不降维——
    上一版的「一笔委托被整笔撤销」就是 1:1，等于把 orders 表挪个地方。"""
    with pytest.raises(ValueError, match="单行属性判断是过滤，不是提取"):
        Relation(roles=("一笔被整笔撤销的委托",), invariant="撤单标志为真")


def test_坍缩比不得小于等于一() -> None:
    with pytest.raises(ValueError, match="这条提取器没有降维"):
        Density(rows_per_symbol_day=1000, collapse_ratio=1.0, receipt="r1")


def test_实测密度必须带收据() -> None:
    with pytest.raises(ValueError, match="必须带收据"):
        Density(rows_per_symbol_day=10, collapse_ratio=50, receipt="")


def test_未实测的条目拒绝进入全量提取() -> None:
    """「预算是拍的」必须在花掉 15 小时之前暴露，而不是之后。"""
    with pytest.raises(ValueError, match="还没在真实数据上测过密度"):
        _seed().require_density()
    ok = _seed(density=Density(rows_per_symbol_day=12, collapse_ratio=400, receipt="r1"))
    assert ok.require_density().collapse_ratio == 400


def test_第一批还没测过密度() -> None:
    """这不是待办事项的委婉说法，是一个诚实的「不知道」。实测填上后本测试须一并改。"""
    assert set(unmeasured()) == set(kinds())
    assert all(s.density is None for s in SEEDS)


def test_每条种子都说得出跨行关系() -> None:
    for s in SEEDS:
        assert len(s.relation.roles) >= 2, s.kind
        assert s.relation.invariant


# ------------------------------------------------- 参数必须可执行

def test_参数值必须是数而不是一句描述() -> None:
    """上一版把 VolClockBar 的刻度写成「daily_ref.volume 的 lookback_days 日均值」——
    照它跑一天只有一根 bar，而 28 个测试全绿。"""
    with pytest.raises(ValueError, match="参数必须可执行"):
        Param("tick", ParamRole.SAMPLING_RESOLUTION, "日均值", "股", "看起来像个参数")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="参数必须可执行"):
        Param("flag", ParamRole.SAMPLING_RESOLUTION, True, "—", "布尔不是刻度")  # type: ignore[arg-type]


def test_判断阈值不得进注册表() -> None:
    with pytest.raises(ValueError, match="判断阈值属于预注册"):
        Param("theta", ParamRole.DECISION_THRESHOLD, 0.9, "分位", "想混进来")


def test_召回闸必须登记召回覆盖率实测() -> None:
    with pytest.raises(ValueError, match="未登记召回覆盖率实测"):
        Param("gate", ParamRole.CANDIDATE_RECALL_GATE, 3, "笔", "调紧单调减少候选")


def test_全部提取参数可枚举进证据指纹() -> None:
    params = extraction_params()
    assert set(params) == set(kinds())
    for kind, ps in params.items():
        for p in ps:
            assert p.role is not ParamRole.DECISION_THRESHOLD, kind
            assert isinstance(p.value, int | float), kind


# ------------------------------------------------- 枚举必须单源

def test_枚举取值必须有类型() -> None:
    """枚举取值只写在 docstring 里等于没有单源。"""
    with pytest.raises(ValueError, match="kind='enum' 与 enum_type 必须同时给出"):
        Measure("side", "enum", "枚举", "1 买 / −1 卖")
    with pytest.raises(ValueError, match="kind='enum' 与 enum_type 必须同时给出"):
        Measure("vol", "int", "股", "量", enum_type=Side)


def test_日界事件的归因码复用缺陷账本() -> None:
    """CI 已校验 DefectCode 等于账本 code 集合；这里不得另起一份。
    上一版写的是 list[str] 加一句「与缺陷账本共用 DefectCode」，一行都没 import。"""
    by_name = {m.name: m for m in day_boundary().measures}
    assert by_name["gap_codes"].enum_type is DefectCode
    assert by_name["gap_codes"].repeated
    assert by_name["coverage_status"].enum_type is CoverageStatus


def test_日界事件缺了单源枚举就构造失败() -> None:
    from ftbv2.core.registry import DayBoundarySpec
    with pytest.raises(ValueError, match="必须用 CoverageStatus 枚举"):
        DayBoundarySpec("DayBoundary", (Measure("coverage_status", "int", "枚举", "状态"),))
    with pytest.raises(ValueError, match="必须是 DefectCode 的重复字段"):
        DayBoundarySpec("DayBoundary", (
            Measure("coverage_status", "enum", "枚举", "状态", enum_type=CoverageStatus),
            Measure("gap_codes", "str", "码", "归因"),
        ))


def test_安静与缺口是两种信息() -> None:
    """安静 = COVERED 且 n_events == 0；缺口 = ABSENT。二者不得合并。"""
    assert {s.value for s in CoverageStatus} == {"covered", "partial", "absent"}
    assert {m.name for m in day_boundary().measures} >= {"coverage_status", "gap_codes", "n_events"}


# ------------------------------------------------- 回看范围

def test_周期型不得成为事件条目() -> None:
    with pytest.raises(ValueError, match="周期型不产出事件行"):
        _seed(lookback=Lookback.CYCLE)


def test_跨日型必须写得出天数() -> None:
    with pytest.raises(ValueError, match="写不出一个数的不是跨日型"):
        _seed(lookback=Lookback.CROSS_DAY, daily_ref_columns=("volume",))


def test_跨日型必须声明读哪些日级参考层列() -> None:
    with pytest.raises(ValueError, match="没声明读哪些日级参考层列"):
        _seed(lookback=Lookback.CROSS_DAY, lookback_days=20)


def test_非跨日型不得带跨日字段() -> None:
    with pytest.raises(ValueError, match="不是跨日型却带了"):
        _seed(lookback=Lookback.INTRA_DAY, lookback_days=5)
    with pytest.raises(ValueError, match="不是跨日型却带了"):
        _seed(lookback=Lookback.INTRA_EPISODE, daily_ref_columns=("volume",))


# ------------------------------------------------- 表与查询

def test_类型名唯一且是合法表名() -> None:
    ks = kinds()
    assert len(set(ks)) == len(ks)
    assert all(k.isidentifier() for k in ks)


def test_未登记的类型查不到而不是返回空() -> None:
    with pytest.raises(KeyError, match="未登记的事件类型"):
        spec("不存在的事件")


def test_几何度量不得重名() -> None:
    m = Measure("vol", "int", "股", "委托量")
    with pytest.raises(ValueError, match="几何度量重名"):
        _seed(measures=(m, m))


def test_stream必须已登记() -> None:
    with pytest.raises(ValueError, match="含未登记流"):
        _seed(streams=("level3",))
    with pytest.raises(ValueError, match="为空或含未登记流"):
        _seed(streams=())
    assert all(st in STREAMS for s in SEEDS for st in s.streams)


def test_日界事件不是注册表条目() -> None:
    assert "DayBoundary" not in kinds()
    assert day_boundary().kind == "DayBoundary"


# ------------------------------------------------- 第一批的选择标准

def test_第一批覆盖每一类提取器() -> None:
    """选择标准不是「便宜」，是每类提取器至少一条——只做段内型，第二批一来跨日型就得改接口。"""
    assert {s.shape for s in SEEDS} == {Shape.INTERVAL, Shape.GROUP, Shape.INSTANT}
    assert {s.lookback for s in SEEDS} == {Lookback.INTRA_DAY, Lookback.CROSS_DAY}
    assert any(len(s.streams) > 1 for s in SEEDS), "缺多流对齐的条目，驱动层的对齐能力没被验证"


def test_三条反常结构都要读成交流() -> None:
    """本次审计的结果：假墙要 trades 确认零成交，冰山要 trades 确认「补单在成交之后」，
    隐藏深度要 trades 算成交量。上一版假墙与冰山只读 orders——**正因为没看成交，切出来的不是那个结构**。"""
    for s in SEEDS:
        if s.kind != "VolClockBar":
            assert "trades" in s.streams, f"{s.kind} 不读成交流，它凭什么知道有没有成交"


# ------------------------------------------------- 版本与摘要

def test_摘要没有被静默改动() -> None:
    assert digest() == REGISTRY_DIGEST, (
        "注册表条目变了。这不是坏事，但必须是有意的：同时更新 REGISTRY_VERSION 与本测试的金标准。"
        f"当前 {digest()}，金标准 {REGISTRY_DIGEST}"
    )


def test_改切割规则会改摘要(monkeypatch: pytest.MonkeyPatch) -> None:
    """摘要真的绑住了条目内容，而不只是绑住版本号字符串——把一条种子的结段规则换掉，摘要必须变。"""
    before = digest()
    改过的 = SEEDS[0].__class__(**{**SEEDS[0].__dict__, "close_rule": "换一条结段规则"})
    monkeypatch.setattr("ftbv2.core.registry.registry.SEEDS", (改过的, *SEEDS[1:]))
    assert digest() != before


def test_版本号已随重写而变() -> None:
    """0.1.0 → 0.2.0：假墙与冰山的切割规则由单行过滤改为跨行结构关系，属改切割算法。"""
    assert version() == "0.2.0"
