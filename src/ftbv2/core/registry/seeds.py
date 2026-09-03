"""第一批种子。选择标准不是「便宜」，是**每类提取器至少一条**——只做段内型，第二批一来跨日型
就得改接口，那才是真返工。四条覆盖：区间/段内/单流 · 组/段内/单流 · 时刻/日内/多流 · 区间/跨日/读日级参考层。
日界事件不在其中：它由驱动层产生，不是注册表条目的产物（schema 见 DAY_BOUNDARY）。

每条都只记几何度量、不带阈值。「多大算大、几次算多、离最优价多近算近」全部是因子层的预注册参数。
"""

from __future__ import annotations

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

_SIDE = Measure("side", "int8", "枚举", "本方方向：1 买 / −1 卖。取自委托或成交的买卖标志，不做推断")
_PRICE = Measure("price", "int64", "元 × 10000", "该结构所在价位，定点整数（core.raw.PRICE_SCALE 同源）")


QUOTE_THEN_WITHDRAW = EventSpec(
    kind="QuoteThenWithdraw",
    alias="假墙撤单",
    shape=Shape.INTERVAL,
    lookback=Lookback.INTRA_EPISODE,
    streams=("orders",),
    open_rule="一笔委托被挂出（新增委托）",
    close_rule="该笔委托被整笔撤销（部分成交后撤余量的不算：那是另一种结构，不在本条目内）",
    measures=(
        _SIDE,
        _PRICE,
        Measure("vol", "int64", "股", "挂出时的委托量。多大算大是因子层的事，这里只记多大"),
        Measure(
            "rest_ms", "int64", "毫秒",
            "从挂出到被整笔撤销经过的毫秒。**不设上界**——「几秒内撤才算假墙」是判断，归因子层。"
            "原种子名 QuoteThenWithdrawWithinT 里的 T 正是这样一个时间阈值，2026-09-03 裁定后去掉",
        ),
        Measure(
            "ticks_from_touch_at_cancel", "int32", "最小价位变动",
            "撤销时刻该价位离本方最优价几个 tick。离最优价多近算近是判断，这里只记几个",
        ),
    ),
    params=(),
    contamination=Contamination.KNOWS_VERDICT,
    v1_audit=(
        "V1 §F：撤单残差为真但稀有闸不显著。不继承那个闸——V1 把「够不够稀有」焊死在提取期，"
        "本条目只切结构、不判显著性，稀有性由因子层在秒级重跑的那一层裁"
    ),
    max_rows_per_symbol_day=200_000,
    expected_rows_per_symbol_day=20_000,
)
"""区间 · 段内 · orders 一流。**已知的体积风险**：去掉时间上界后，每一笔被整笔撤销的委托都是一条事件。
A 股撤单率不低，这条很可能是四条里最大的一张表。预算 20 万条/(标的,日) 是拍的，第一片跑完拿真数字，
超了就带着实测回来重议——不靠猜，靠量。"""


SEQ_REPEATED_SAME_PX_VOL = EventSpec(
    kind="Seq_RepeatedSamePxVol",
    alias="冰山",
    shape=Shape.GROUP,
    lookback=Lookback.INTRA_EPISODE,
    streams=("orders",),
    open_rule=(
        "出现同 (side, price, vol) 的第 2 笔本方委托，且与第 1 笔相邻（中间无其他本方委托）。"
        "2 是「成组」的词义下限，不是阈值：1 笔不成组；≥ 3 才是判断，归因子层"
    ),
    close_rule=(
        "出现一笔价或量与本组不同的本方委托，或方向反转。**不得写「隔了 T 久没动静就结组」**——"
        "庄家不掐表，是子弹打光了或者量到了。代价是有人整天同价同量报单时这一组横跨全天，"
        "那本来就是事实，由因子层看 n_slices 与 span_ms 自己判断"
    ),
    measures=(
        _SIDE,
        _PRICE,
        Measure("slice_vol", "int64", "股", "每片的委托量。本组内恒定——它是成组的条件之一"),
        Measure("n_slices", "int32", "笔", "本组一共几片。几片算多是判断，这里只记几片"),
        Measure(
            "span_ms", "int64", "毫秒",
            "首片到末片的毫秒跨度。组不是区间：中间可能夹着别人的委托，这只是首尾之差",
        ),
    ),
    params=(),
    contamination=Contamination.KNOWS_VERDICT,
    v1_audit=(
        "V1 §C：REP_MIN=10 是错代理，96% 的触发来自它。不继承——把 10 改成 3 只是把同一段判断挪个位置，"
        "所以本条目**不设 k**，只记 n_slices，由因子层裁"
    ),
    max_rows_per_symbol_day=50_000,
    expected_rows_per_symbol_day=2_000,
)
"""组 · 段内 · orders 一流。与假墙同为段内单流，但几何度量完全不同——这一条在第一批里的职责
是验证「每类事件一张表」撑不撑得住。"""


SNAPSHOT_REST_GEQ_DISP = EventSpec(
    kind="SnapshotRestGeqDisp",
    alias="隐藏深度",
    shape=Shape.INSTANT,
    lookback=Lookback.INTRA_DAY,
    streams=("xinqing", "trades"),
    open_rule=(
        "相邻两快照之间该价位的成交量 ≥ 前一快照在该价位的展示量，且该档价位未跳。"
        "≥ 1 倍即记（存在性），不设倍数——「几倍算藏得多」是判断"
    ),
    close_rule="同一时刻即结束：这是时刻型，start_time == end_time，没有跨度",
    measures=(
        _SIDE,
        _PRICE,
        Measure("displayed_vol", "int64", "股", "前一快照在该价位的展示量"),
        Measure("executed_vol", "int64", "股", "两快照之间在该价位的成交量"),
    ),
    params=(),
    contamination=Contamination.KNOWS_VERDICT,
    v1_audit=(
        "V1 §D 承接：隐藏深度的结构本身没被证伪，被证伪的是它的方向性解读。"
        "本条目只记 displayed_vol 与 executed_vol 两个数，方向留给因子层"
    ),
    max_rows_per_symbol_day=100_000,
    expected_rows_per_symbol_day=5_000,
)
"""时刻 · 日内 · 两流对齐。第一批里唯一的多流条目——它的职责是验证驱动层能不能把 xinqing 与
trades 在同一天里对齐着喂给同一个提取器。D013（供应商在 09:30 / 13:00 每股各发两帧重复开盘快照）
必须在进入本条目之前由形状消歧层归一，注册表不知道 D013 存在。"""


VOL_CLOCK_BAR = EventSpec(
    kind="VolClockBar",
    alias="成交量时钟",
    shape=Shape.INTERVAL,
    lookback=Lookback.CROSS_DAY,
    streams=("trades",),
    open_rule="上一根 bar 结束的那一笔成交之后的第一笔成交",
    close_rule=(
        "本 bar 累计成交量跨越刻度 tick_volume。**量到了就结**，不是时间到了就结——"
        "这正是「庄家不掐表」那条规矩在切割规则上的样子"
    ),
    measures=(
        Measure("bar_index", "int32", "序号", "当日第几根 bar，从 0 起"),
        Measure("volume", "int64", "股", "本 bar 的累计成交量。≥ tick_volume，末根可能不足（当日收盘截断）"),
        Measure("vwap", "int64", "元 × 10000", "本 bar 的成交量加权均价，定点整数"),
        Measure("n_trades", "int32", "笔", "本 bar 内的成交笔数"),
    ),
    params=(
        Param(
            name="tick_volume_source",
            role=ParamRole.SAMPLING_RESOLUTION,
            value="daily_ref.volume 的 lookback_days 日均值",
            unit="—",
            why=(
                "刻度是采样分辨率：调它只让 bar 更粗或更细，不会把 bar 换成另一种东西（单调同伦）；"
                "它不含任何关于「该量在样本中的排名」的判断（维度切分）；怎么取都至少切得出一根 bar（不可调出空集）。"
                "刻度来自 T−1 及以前，**绝不含当日自身**——否则 09:35 的 bar 边界会取决于 14:55，"
                "事件流的存在性本身就携带未来信息"
            ),
        ),
    ),
    contamination=Contamination.KNOWS_VERDICT,
    v1_audit=(
        "V1 accum_dist 的 bar 承接。V1 的 bar 与阈值焊在同一段代码里（阈值算在生成期），"
        "本条目只产 bar 与其几何，不产任何判定"
    ),
    max_rows_per_symbol_day=2_000,
    expected_rows_per_symbol_day=200,
    lookback_days=20,
    daily_ref_columns=("volume",),
)
"""区间 · 跨日 · 读日级参考层。第一批里唯一的跨日条目——它的职责是验证「注册表声明要什么、
驱动层决定怎么跑」这套分工立不立得住。跨日不额外贵：原始层按天存在 4 IOPS 的机械盘上，
外层按日期升序本来就是最快读法，T−1 因此在手边。冷启动（前 lookback_days 天）记缺口，不进事件流。"""


DAY_BOUNDARY = DayBoundarySpec(
    kind="DayBoundary",
    measures=(
        Measure(
            "coverage_status", "int8", "枚举",
            "该 (标的, 交易日) 的覆盖状态：有数据无结构（安静）/ 停牌心跳 / 无原始数据 / 单边缺失 / 救援日",
        ),
        Measure(
            "gap_codes", "list[str]", "缺陷账本 DefectCode",
            "缺口的归因码，与缺陷账本共用 DefectCode。「查不到 = 没有」被禁止：缺口必须携带归因",
        ),
        Measure(
            "n_events", "int32", "条",
            "当日该标的的事件条数（不含本条）。因子层的跨日累积从这里起步，不必回看原始层",
        ),
        Measure("quiet_span_ms", "int64", "毫秒", "当日最长的无事件跨度。V1 唯一存活几何「持续安静」的原料"),
    ),
)
"""日界事件的 schema。**不是注册表条目**——驱动层从 交易日历 × 标的主表 × 摄取收据的叉积产生。
每个 (标的, 交易日) 恰好一条，没有任何结构的安静日也让因子状态机前进一步。
window_stats（n_events / quiet_span_ms）在这里，因子层直接读，不必自己扫全天。"""


SEEDS: tuple[EventSpec, ...] = (
    QUOTE_THEN_WITHDRAW,
    SEQ_REPEATED_SAME_PX_VOL,
    SNAPSHOT_REST_GEQ_DISP,
    VOL_CLOCK_BAR,
)
