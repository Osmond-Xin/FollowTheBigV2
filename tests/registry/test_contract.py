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

from ftbv2.core.raw import CONTINUOUS_EXCL_AUCTIONS, STREAMS, DefectCode, Window
from ftbv2.core.registry import (
    SEEDS,
    TOTAL_ORDER,
    Contamination,
    CoverageStatus,
    DensityMeasurement,
    DensityTarget,
    EventClass,
    EventSpec,
    EvidenceRef,
    InvariantCode,
    Lookback,
    Measure,
    MeasureRole,
    Param,
    ParamRole,
    Relation,
    Shape,
    Side,
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
    yields_events,
)

REGISTRY_DIGEST = "c84cfa7b28f99cb7"
"""金标准摘要（含每个 enum_type 的取值集合）。改任何一条条目都会让它变——
**改摘要必须同时改 REGISTRY_VERSION**。这条是本文件里最有用的一个测试：
它不判断对错，只保证没有人能悄悄改掉定义。"""

_REL = Relation(roles=("甲", "乙"), invariants=(InvariantCode.RETURNS_TO_ZERO,), doc="甲在乙之前")
_TARGET = DensityTarget(max_rows_per_symbol_day=30.0, min_collapse_ratio=1000.0, basis="立项成本口径")


def _evidence(**over: object) -> EvidenceRef:
    base = {"receipt_id": "a" * 64, "input_manifest_sha256": "b" * 64,
            "extractor_commit": "c" * 40, "spec_digest": "deadbeef", "sort_key": TOTAL_ORDER}
    return EvidenceRef(**(base | over))          # type: ignore[arg-type]


def _measurement(kind: str = "LevelBuildThenVanish", rows: float = 5.0, ratio: float = 10000.0
                 ) -> DensityMeasurement:
    return DensityMeasurement(kind=kind, rows_per_symbol_day=rows, collapse_ratio=ratio,
                              symbol_days=4200, input_rows=246_088_066, event_rows=23_212,
                              evidence=_evidence())


def _seed(**over: object) -> EventSpec:
    base = {
        "kind": "Probe", "alias": "探针", "event_class": EventClass.BAR, "shape": Shape.INTERVAL,
        "lookback": Lookback.INTRA_EPISODE, "streams": ("orders",),
        "windows": CONTINUOUS_EXCL_AUCTIONS, "total_order": TOTAL_ORDER,
        "relation": _REL, "open_rule": "甲出现", "close_rule": "乙出现",
        "measures": (Measure("vol", "int", "股", "委托量", MeasureRole.CANDIDATE_VARIABLE),), "params": (),
        "contamination": Contamination.UNAWARE, "v1_audit": "V1 未做过此结构",
    }
    return EventSpec(**(base | over))          # type: ignore[arg-type]


# ------------------------------------------------- 单行过滤不是提取（本次审计的核心）

def test_结构关系至少两个角色() -> None:
    """一条事件必须由多条原始记录的关系构成。单行属性判断是过滤，不降维——
    上一版的「一笔委托被整笔撤销」就是 1:1，等于把 orders 表挪个地方。"""
    with pytest.raises(ValueError, match="单行属性判断是过滤，不是提取"):
        Relation(roles=("一笔被整笔撤销的委托",), invariants=(InvariantCode.RETURNS_TO_ZERO,),
                 doc="撤单标志为真")


def test_坍缩比下界写成一等于没有下界() -> None:
    with pytest.raises(ValueError, match="等于没有下界"):
        DensityTarget(max_rows_per_symbol_day=30.0, min_collapse_ratio=1.0, basis="随便写的")


def test_结构事件必须写下密度目标() -> None:
    """不写成本上界与坍缩下界，就没有任何东西能在花掉 15 小时之前拦住爆量。"""
    with pytest.raises(ValueError, match="是结构事件却没有密度目标"):
        _seed(event_class=EventClass.STRUCTURAL_EVENT)


def test_尺子与参考层不得套密度目标() -> None:
    """成交量时钟一天固定 48 根是设计出来的，对它谈稀有性没有意义。"""
    with pytest.raises(ValueError, match="却带了密度目标"):
        _seed(event_class=EventClass.BAR, density_target=_TARGET)


def test_实测密度不在条目上() -> None:
    """红队 2026-09-03 架构严重 5：实测值进静态源码 = 有人要反向修改源码文件，
    与「代码保持只读」和收据机制死锁。纯核只留目标，实测走收据。"""
    assert not hasattr(spec("LevelBuildThenVanish"), "density")
    assert spec("LevelBuildThenVanish").density_target is not None


def test_未实测的条目拒绝进入全量提取() -> None:
    """「预算是拍的」必须在花掉 15 小时之前暴露，而不是之后。"""
    with pytest.raises(ValueError, match="还没在真实数据上测过密度"):
        admit_full_extraction("LevelBuildThenVanish", None)


def test_超过成本上界的实测被拒() -> None:
    with pytest.raises(ValueError, match="超过上界"):
        admit_full_extraction("LevelBuildThenVanish", _measurement(rows=45.0))


def test_降维不足的实测被拒() -> None:
    with pytest.raises(ValueError, match="降维不足"):
        admit_full_extraction("LevelBuildThenVanish", _measurement(ratio=900.0))


def test_拿错收据会被认出来() -> None:
    with pytest.raises(ValueError, match="拿错了收据"):
        admit_full_extraction("LevelBuildThenVanish", _measurement(kind="RefillAfterFill"))


def test_尺子不走密度准入() -> None:
    with pytest.raises(ValueError, match="不走密度准入"):
        admit_full_extraction("VolClockBar", _measurement(kind="VolClockBar"))


def test_假墙的实测准入通过() -> None:
    """2026-09-03 实测：21 个样本日 × 200 标的，5.34 条/(标的·日)，坍缩约 1.1 万倍。
    收据见 `.lineage/receipts/`，数字见 design-log。**这个数不写进源码**，
    本测试用的是同一口径的等价值，只验准入逻辑本身放行它。"""
    got = admit_full_extraction("LevelBuildThenVanish", _measurement(rows=5.34, ratio=11_000.0))
    assert got.event_rows > 0


def test_血缘必须结构化而不是一句measured() -> None:
    """红队 2026-09-03 工程严重 4：`receipt: str` 非空是门面。"""
    with pytest.raises(ValueError, match="不是 64 位十六进制"):
        _evidence(receipt_id="measured")
    with pytest.raises(ValueError, match="不是 40 位十六进制"):
        _evidence(extractor_commit="HEAD")
    with pytest.raises(ValueError, match="缺排序键"):
        _evidence(sort_key=())


def test_第一批的结构事件还没测过密度() -> None:
    """实测住在收据里，所以「测没测过」要拿着收据问，不是问源码。"""
    assert set(unmeasured({})) == set(structural_events())
    measured = {"LevelBuildThenVanish": _measurement()}
    assert set(unmeasured(measured)) == set(structural_events()) - {"LevelBuildThenVanish"}


def test_每条种子都说得出跨行关系() -> None:
    for s in SEEDS:
        assert len(s.relation.roles) >= 2, s.kind
        assert s.relation.invariants, s.kind


def test_不变量不再是一句话() -> None:
    """红队三方一致：`invariant: str` 只查非空，写 TODO 也过。现在每条挂可跑的谓词。"""
    for s in SEEDS:
        assert all(isinstance(c, InvariantCode) for c in s.relation.invariants), s.kind
        assert s.relation.required_columns(), s.kind


def test_空不变量的结构关系构造失败() -> None:
    with pytest.raises(ValueError, match="一组空的不变量恒真"):
        Relation(roles=("甲", "乙"), invariants=(), doc="什么都不要求")


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
        Measure("side", "enum", "枚举", "1 买 / −1 卖", MeasureRole.IDENTITY)
    with pytest.raises(ValueError, match="kind='enum' 与 enum_type 必须同时给出"):
        Measure("vol", "int", "股", "量", MeasureRole.CANDIDATE_VARIABLE, enum_type=Side)


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
        DayBoundarySpec("DayBoundary", (
            Measure("coverage_status", "int", "枚举", "状态", MeasureRole.IDENTITY),))
    with pytest.raises(ValueError, match="必须是 DefectCode 的重复字段"):
        DayBoundarySpec("DayBoundary", (
            Measure("coverage_status", "enum", "枚举", "状态", MeasureRole.IDENTITY,
                    enum_type=CoverageStatus),
            Measure("gap_codes", "str", "码", "归因", MeasureRole.IDENTITY),
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
    m = Measure("vol", "int", "股", "委托量", MeasureRole.CANDIDATE_VARIABLE)
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
    """0.2.0 → 0.3.0：不变量由字符串改为可执行谓词、实测密度移出条目、
    假墙加「峰值时刻十档内可见」、成交量时钟刻度改成交额——都属改切割算法。"""
    assert version() == "0.3.0"


# ------------------------------------------------- 适用时段 · 排序键 · 对照裁决（红队 2026-09-03）

def test_没声明适用时段的条目构造失败() -> None:
    """集合竞价允许撤单且撮合未开始，「建档、撤回零、期间零成交」会整批误报。"""
    with pytest.raises(ValueError, match="没声明适用时段"):
        _seed(windows=())


def test_要集合竞价可以但必须说明() -> None:
    """集合竞价里的挂撤本身是研究对象，要它不是错——错的是不声明就把两种样本混在一起。"""
    auction = (Window(9 * 3600 * 1000 + 15 * 60 * 1000, 9 * 3600 * 1000 + 25 * 60 * 1000),)
    with pytest.raises(ValueError, match="没写 auction_reason"):
        _seed(windows=auction)
    ok = _seed(windows=auction, auction_reason="集合竞价的撤单就是本条目的研究对象")
    assert ok.windows == auction


def test_第一批一律排除开盘集合竞价() -> None:
    for s in SEEDS:
        assert s.windows == CONTINUOUS_EXCL_AUCTIONS, s.kind
        assert s.auction_reason is None, s.kind


def test_没声明排序键的提取器不可复现() -> None:
    with pytest.raises(ValueError, match="没声明排序键"):
        _seed(total_order=())


def test_排序键不用已知不可靠的成交编号() -> None:
    """`trades.seq` 在缺陷账本里有两条登记：7 天整列空、10 天稀疏重复。
    拿一个已知不可靠的列当全序，等于把不可复现藏起来。"""
    assert "seq" not in TOTAL_ORDER
    assert all(s.total_order == TOTAL_ORDER for s in SEEDS)


def test_每条种子都安排了对照裁决() -> None:
    """与密度同级的一道门禁：四条由同一人在同一上下文里写完且全部 KNOWS_VERDICT，
    没有独立裁决就没有东西拦得住 V1 的判断被整批继承（红队 2026-09-03 方法论致命 3）。"""
    assert uncontrasted() == ()
    for s in SEEDS:
        assert s.require_contrast(), s.kind


def test_没有对照裁决的条目拒绝放行() -> None:
    with pytest.raises(ValueError, match="没有对照裁决"):
        _seed().require_contrast()


# ------------------------------------------------- 判断不进事件流 · 多重检验作用域

def test_带判断的字段名进不了事件流() -> None:
    """红队 2026-09-03 工程建议 1：把「事件不含判断」从注释升级成构造约束。"""
    for bad in ("peak_vol_rank", "score", "is_abnormal", "vol_threshold", "vol_分位", "life_ms_排名"):
        with pytest.raises(ValueError, match="的名字里带判断"):
            Measure(bad, "int", "股", "混进来的判断", MeasureRole.CANDIDATE_VARIABLE)


def test_备选变量的总数是可数的() -> None:
    """**这个数就是多重检验作用域的下界**：加一个字段就得改它，让组合空间在预注册前可见。
    红队方法论严重 5 估的是 4 × 9 = 36，数出来是 19。"""
    per_kind = candidate_variables()
    assert set(per_kind) == set(kinds())
    assert sum(len(v) for v in per_kind.values()) == 19
    for kind, names in per_kind.items():
        assert names, f"{kind} 一个备选变量都没有：那它的事件流没有任何东西给因子层用"


def test_定位字段不算备选变量() -> None:
    """side / price / 对齐误差是用来定位与自证的，不是因子输入。"""
    wall = spec("LevelBuildThenVanish")
    assert "side" not in wall.candidate_variables()
    assert "frame_age_ms" not in wall.candidate_variables()
    assert "peak_vol" in wall.candidate_variables()


# ------------------------------------------------- 停牌心跳与缺口短路

def test_停牌心跳日不产事件而不是挂住() -> None:
    """那天只有行情、没有委托没有成交，读 orders / trades 的提取器会在等成交上挂住。"""
    wall = spec("LevelBuildThenVanish")
    assert not yields_events(wall, CoverageStatus.COVERED, (DefectCode.QUOTE_ONLY,))
    assert yields_events(wall, CoverageStatus.COVERED, ())


def test_没有任何数据的天不产事件() -> None:
    assert not yields_events(spec("LevelBuildThenVanish"), CoverageStatus.ABSENT, ())


def test_停牌心跳不影响不读逐笔的条目() -> None:
    """短路的理由是「读的那两条流那天没有」，不是「这一天不算数」——理由不成立就不短路。"""
    only_quotes = _seed(streams=("xinqing",))
    assert yields_events(only_quotes, CoverageStatus.COVERED, (DefectCode.QUOTE_ONLY,))


# ------------------------------------------------- 假墙的可见性裁决（2026-09-03 实测）

def test_假墙必须在峰值时刻看得见() -> None:
    """21 个样本日实测：十档外占候选 87%，中位 9 笔委托堆起 5 千股；十档内是 40 余笔堆起
    2–7 万股。两类形态完全不同——**看不见的墙吓不到人**。这是存在性约束不是幅度阈值：
    十档是交易所定的发布范围，不是我们选的一个数。"""
    wall = spec("LevelBuildThenVanish")
    assert InvariantCode.VISIBLE_IN_QUOTED_DEPTH_AT_PEAK in wall.relation.invariants
    assert "xinqing" in wall.streams, "判可见性要读十档快照"


def test_假墙不再靠必须到过最优价() -> None:
    """实测否定了这一刀：只剩 1.6 条/(标的·日)，98% 的结构被扔掉。该切的一刀是可见性。"""
    wall = spec("LevelBuildThenVanish")
    assert not any("touch" in c.value for c in wall.relation.invariants)
    assert "ticks_from_touch_at_nearest_frame" in wall.candidate_variables(), \
        "离最优价多远仍然记下来，只是不拿它当切割规则"


def test_隐藏深度补上了新增委托这一条() -> None:
    """3 秒穿档再被新挂单撑住，后一帧的「活着」是新来的人（红队架构严重 4）。"""
    hidden = spec("FillExceedsDisplayed")
    assert InvariantCode.NO_NEW_ORDERS_BETWEEN_FRAMES in hidden.relation.invariants
    assert "orders" in hidden.streams, "判「有没有新挂单」单靠快照 + 成交物理上做不到"


def test_成交量时钟的刻度改用成交额() -> None:
    """高送转当日成交股数天然翻倍，用未复权的成交量做刻度会让当日切出近百根 bar
    （红队架构严重 7）。成交额不受送转影响。"""
    bar = spec("VolClockBar")
    assert bar.daily_ref_columns == ("amount",)
    assert bar.params[0].source == "daily_ref.amount"
    assert bar.event_class is EventClass.BAR


def test_一条都没切出来不算量过了() -> None:
    """坍缩比这时是无穷大——放它过去等于用一个除零的数字批准 15 小时。"""
    empty = DensityMeasurement(kind="LevelBuildThenVanish", rows_per_symbol_day=0.0,
                               collapse_ratio=float("inf"), symbol_days=200, input_rows=13_928_633,
                               event_rows=0, evidence=_evidence())
    with pytest.raises(ValueError, match="一条都没切出来"):
        admit_full_extraction("LevelBuildThenVanish", empty)
