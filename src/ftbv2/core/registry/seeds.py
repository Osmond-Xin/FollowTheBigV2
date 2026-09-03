"""第一批种子。选择标准是**每类提取器至少一条**——只做段内型，第二批一来跨日型就得改接口。
四条覆盖：区间/日内/双流 · 组/日内/双流 · 时刻/日内/双流 · 区间/跨日/读日级参考层。
日界事件不在其中：它由驱动层产生，不是注册表条目的产物（schema 见 DAY_BOUNDARY）。

**每条的切割规则都是多条原始记录之间的关系，不是单行属性判断。** 2026-09-03 审计推翻了上一版的
`QuoteThenWithdraw`（「一笔委托被整笔撤销」——撤单标志就在那一行上，1:1，等于把 orders 表挪个地方）
与 `Seq_RepeatedSamePxVol`（「同价同量连着报两笔」——密集报单里满地都是，不是冰山的机制）。

**四条的 density 全是 None：还没在真实数据上测过。** 这是诚实的未知，不是待办事项的委婉说法——
`require_density()` 会拦住任何未实测条目进入全量提取。
"""

from __future__ import annotations

from ftbv2.core.raw import DefectCode
from ftbv2.core.registry.types import (
    Contamination,
    CoverageStatus,
    DayBoundarySpec,
    EventSpec,
    Lookback,
    Measure,
    Param,
    ParamRole,
    Relation,
    Shape,
    Side,
)

_SIDE = Measure("side", "enum", "枚举", "本方方向，取自委托或成交的买卖标志，不做推断", enum_type=Side)
_PRICE = Measure("price", "price", "元 × 10000", "该结构所在价位，定点整数（core.raw.PRICE_SCALE 同源）")


LEVEL_BUILD_THEN_VANISH = EventSpec(
    kind="LevelBuildThenVanish",
    alias="假墙",
    shape=Shape.INTERVAL,
    lookback=Lookback.INTRA_DAY,
    streams=("orders", "trades"),
    relation=Relation(
        roles=("建墙的本方委托", "拆墙的本方撤单", "该档位存续期间的成交"),
        invariant=(
            "同一 (标的, side, price) 上：一批委托把该档位从无堆到有，另一批撤单把它清回零并使该档位"
            "从盘口消失，且第三个角色在整个存续期间为**空集**（该档位一笔没成交）。"
            "三个角色缺一不成立——尤其第三个：有成交的档位消失是被吃掉的，不是被撤走的，那是另一回事"
        ),
    ),
    open_rule="某 (side, price) 档位在十档可见范围内从无到有被本方委托建立",
    close_rule=(
        "该档位深度回到零并从盘口消失。**墙没了这件事本身是结构终点**，不是「隔了多久」——"
        "庄家不掐表，是子弹打光了或者量到了"
    ),
    measures=(
        _SIDE,
        _PRICE,
        Measure("peak_vol", "int", "股", "该档位存续期间达到的最大展示深度。多大算大是因子层的事"),
        Measure("n_orders", "int", "笔", "把这个档位堆起来用了几笔委托。1 笔也成立，但那是最弱的一种墙"),
        Measure("n_cancels", "int", "笔", "把它拆掉用了几笔撤单"),
        Measure("build_ms", "int", "毫秒", "从档位出现到达到 peak_vol 的毫秒"),
        Measure("life_ms", "int", "毫秒", "从档位出现到它消失的毫秒。不设上界——「几秒内撤才算假墙」是判断"),
        Measure("teardown_ms", "int", "毫秒", "从 peak_vol 到档位消失的毫秒。拆得比堆得快是几何事实，不是判定"),
        Measure(
            "ticks_from_touch_at_peak", "int", "最小价位变动",
            "peak 时刻该档位离本方最优价几个 tick。离得多近算近是判断，这里只记几个",
        ),
    ),
    params=(),
    contamination=Contamination.KNOWS_VERDICT,
    v1_audit=(
        "V1 §F：撤单残差为真但稀有闸不显著。不继承那个闸——V1 把「够不够稀有」焊死在提取期。"
        "本条目的稀有性来自结构本身（建起来又整个消失且全程零成交），不来自任何闸"
    ),
)
"""区间 · 日内 · orders + trades。**墙不是一笔委托，是一个档位。**

需要日内型是因为盘口深度要从开盘逐笔累积重建；零成交要靠 trades 流确认。
一笔孤立的撤单不再构成事件：绝大多数撤单只让档位变薄，不会让它消失。"""


REFILL_AFTER_FILL = EventSpec(
    kind="RefillAfterFill",
    alias="冰山",
    shape=Shape.GROUP,
    lookback=Lookback.INTRA_DAY,
    streams=("orders", "trades"),
    relation=Relation(
        roles=("被成交耗尽的本方委托", "耗尽它的成交", "同价同量的补单"),
        invariant=(
            "三者按时序咬合并至少重复一轮：委托 → 被成交吃完 → 同 (price, vol) 的新委托出现。"
            "**补单必须发生在成交之后**——这才是冰山的机制。上一版只要求「同价同量相邻两笔」，"
            "那在密集报单里满地都是，与是否成交无关，切出来的不是冰山"
        ),
    ),
    open_rule=(
        "某 (side, price) 上一笔委托被成交耗尽后，同 (price, vol) 的新委托出现——"
        "即第一次完成「成交 → 补单」。一轮即成立：「重复」的词义下限是 2 笔委托，不是阈值"
    ),
    close_rule=(
        "出现价或量与本组不同的本方委托，或该价位被穿过。**不得写「隔了 T 久没补就结组」**。"
        "代价是有人整天在同一价位补同样的量时这一组横跨全天——那本来就是事实，"
        "由因子层看 n_refills 与 span_ms 自己判断"
    ),
    measures=(
        _SIDE,
        _PRICE,
        Measure("slice_vol", "int", "股", "每片的委托量。本组内恒定——它是成组的条件之一"),
        Measure("n_refills", "int", "次", "完成了几轮「成交 → 补单」。几轮算多是判断，这里只记几轮"),
        Measure("total_filled", "int", "股", "本组累计被成交的量。它是 n_refills × slice_vol 的实测校验"),
        Measure("span_ms", "int", "毫秒", "首片到末片的毫秒跨度。组不是区间：中间可能夹着别人的委托"),
    ),
    params=(),
    contamination=Contamination.KNOWS_VERDICT,
    v1_audit=(
        "V1 §C：REP_MIN=10 是错代理，96% 的触发来自它。不继承——把 10 改成 3 只是把同一段判断挪个位置。"
        "本条目不设 k，改用「成交后才补」这个机制约束把稀有性做进结构里，n_refills 交给因子层裁"
    ),
)
"""组 · 日内 · orders + trades。**冰山不是「连着报两笔一样的」，是「成交一片、补一片」。**

需要 trades 流才能知道前一片是不是被吃完了——这正是上一版单读 orders 就写得出来的原因：
它根本没看成交，所以它切的不是冰山。"""


FILL_EXCEEDS_DISPLAYED = EventSpec(
    kind="FillExceedsDisplayed",
    alias="隐藏深度",
    shape=Shape.INSTANT,
    lookback=Lookback.INTRA_DAY,
    streams=("xinqing", "trades"),
    relation=Relation(
        roles=("前一帧快照在该档的展示量", "两帧之间在该档的成交", "后一帧快照上该档仍然存在"),
        invariant=(
            "成交量 ≥ 前一帧的展示量，**而该档位在后一帧仍然活着**。"
            "档位在被吃掉自身全部展示量之后还在，只能是因为它背后有没显示出来的量——"
            "**幸存本身就是隐藏的证据**。上一版写的是「≥ 1 倍即记（存在性）」，"
            "1 是个幅度、只是被起了个『存在性』的名字；真正的结构是第三个角色"
        ),
    ),
    open_rule="相邻两帧之间该档成交量 ≥ 前一帧该档展示量，且该档位在后一帧仍存在于盘口",
    close_rule="同一时刻即结束：时刻型，start_time == end_time，没有跨度",
    measures=(
        _SIDE,
        _PRICE,
        Measure("displayed_vol", "int", "股", "前一帧在该档位的展示量"),
        Measure("executed_vol", "int", "股", "两帧之间在该档位的成交量"),
        Measure("surviving_vol", "int", "股", "后一帧该档位仍显示的量——它是「幸存」这件事的数值证据"),
    ),
    params=(),
    contamination=Contamination.KNOWS_VERDICT,
    v1_audit=(
        "V1 §D 承接：隐藏深度的结构本身没被证伪，被证伪的是它的方向性解读。"
        "本条目只记三个量，方向留给因子层"
    ),
)
"""时刻 · 日内 · xinqing + trades。第一批里 D013（供应商在 09:30 / 13:00 每股各发两帧重复开盘快照）
影响最大的一条：两帧比对撞上重复帧会算出零成交量。归一在形状消歧层做，注册表不知道 D013 存在。"""


VOL_CLOCK_BAR = EventSpec(
    kind="VolClockBar",
    alias="成交量时钟",
    shape=Shape.INTERVAL,
    lookback=Lookback.CROSS_DAY,
    streams=("trades",),
    relation=Relation(
        roles=("上一根 bar 结束后的第一笔成交", "本 bar 内的全部成交", "使累计量跨过刻度的那一笔"),
        invariant="一根 bar 由一段连续成交构成，其累计量首次跨过 tick_volume 的那一笔即为终点",
    ),
    open_rule="上一根 bar 结束的那一笔成交之后的第一笔成交",
    close_rule=(
        "本 bar 累计成交量首次跨越刻度 tick_volume。**量到了就结**，不是时间到了就结——"
        "这正是「庄家不掐表」那条规矩在切割规则上的样子"
    ),
    measures=(
        Measure("bar_index", "int", "序号", "当日第几根 bar，从 0 起"),
        Measure("volume", "int", "股", "本 bar 累计成交量。≥ tick_volume，末根可能不足（当日收盘截断）"),
        Measure("vwap", "price", "元 × 10000", "本 bar 的成交量加权均价，定点整数"),
        Measure("n_trades", "int", "笔", "本 bar 内的成交笔数"),
    ),
    params=(
        Param(
            name="bars_per_day",
            role=ParamRole.SAMPLING_RESOLUTION,
            value=48,
            unit="根 / 交易日",
            why=(
                "刻度 tick_volume = daily_ref.volume 的 lookback_days 日均值 ÷ bars_per_day。"
                "上一版把刻度写成一句描述「daily_ref.volume 的 lookback_days 日均值」——照它跑一天只有"
                "一根 bar，那不是成交量时钟，而 28 个测试全绿。除数是采样分辨率：调它只让 bar 更粗或更细，"
                "不会把 bar 换成另一种东西（单调同伦）；不含任何关于「该量在样本中的排名」的判断（维度切分）；"
                "怎么取都至少切得出一根（不可调出空集）。48 对应连续竞价 4 小时里平均每 5 分钟一根，"
                "是个起点值，第一片实测后按每日 bar 数分布重定。"
                "日均值取自 T−1 及以前，**绝不含当日自身**——否则 09:35 的 bar 边界会取决于 14:55"
            ),
            source="daily_ref.volume",
        ),
    ),
    contamination=Contamination.KNOWS_VERDICT,
    v1_audit=(
        "V1 accum_dist 的 bar 承接。V1 的 bar 与阈值焊在同一段代码里（阈值算在生成期），"
        "本条目只产 bar 与其几何，不产任何判定"
    ),
    lookback_days=20,
    daily_ref_columns=("volume",),
)
"""区间 · 跨日 · trades + daily_ref。第一批里唯一的跨日条目——验证「注册表声明要什么、
驱动层决定怎么跑」这套分工。跨日不额外贵：外层按日期升序本来就是最快读法，T−1 在手边。
冷启动（前 lookback_days 天）记缺口，不进事件流。

它与另外三条不同：**不是「反常」，是一把尺子**——把活跃股与半死不活的股放到同一刻度上比。
密度由 bars_per_day 直接控制，是四条里唯一密度可预测的。"""


DAY_BOUNDARY = DayBoundarySpec(
    kind="DayBoundary",
    measures=(
        Measure(
            "coverage_status", "enum", "枚举",
            "该 (标的, 交易日) 有没有数据。**只答有没有，不答为什么**——归因是 gap_codes 的事",
            enum_type=CoverageStatus,
        ),
        Measure(
            "gap_codes", "enum", "缺陷账本 code",
            "缺口与形状的归因码，取值就是缺陷账本的 DefectCode（CI 校验二者相等）。"
            "「查不到 = 没有」被禁止：缺口必须携带归因",
            repeated=True,
            enum_type=DefectCode,
        ),
        Measure(
            "n_events", "int", "条",
            "当日该标的的事件条数（不含本条）。安静 = COVERED 且 n_events == 0，与缺口是两种信息。"
            "因子层的跨日累积从这里起步，不必回看原始层",
        ),
        Measure("quiet_span_ms", "int", "毫秒", "当日最长的无事件跨度。V1 唯一存活几何「持续安静」的原料"),
    ),
)
"""日界事件的 schema。**不是注册表条目**——驱动层从 交易日历 × 标的主表 × 摄取收据的叉积产生。
每个 (标的, 交易日) 恰好一条，没有任何结构的安静日也让因子状态机前进一步。"""


SEEDS: tuple[EventSpec, ...] = (
    LEVEL_BUILD_THEN_VANISH,
    REFILL_AFTER_FILL,
    FILL_EXCEEDS_DISPLAYED,
    VOL_CLOCK_BAR,
)
