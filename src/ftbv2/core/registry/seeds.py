"""第一批种子。选择标准是**每类提取器至少一条**——只做段内型，第二批一来跨日型就得改接口。
四条覆盖：区间/日内/双流 · 组/日内/双流 · 时刻/日内/三流 · 区间/跨日/读日级参考层。
日界事件不在其中：它由驱动层产生，不是注册表条目的产物（schema 见 DAY_BOUNDARY）。

**每条的切割规则都是多条原始记录之间的关系，不是单行属性判断**，而且不变量不再是一句话：
每条挂一组 `InvariantCode`，每条码在 `predicates.py` 里有可跑的表达式与最小反例单测。

**实测密度不写在这里**（红队 2026-09-03 架构严重 5）。条目只声明目标区间；量出来的数字住在
收据里，由 `registry.admit_full_extraction()` 在下发全量之前比对。
`LevelBuildThenVanish` 的第一次实测见 `docs/design-log/2026-09-03-假墙可见性回归.md`。
"""

from __future__ import annotations

from ftbv2.core.raw import CONTINUOUS_EXCL_AUCTIONS, DefectCode
from ftbv2.core.registry.predicates import InvariantCode as Inv
from ftbv2.core.registry.types import (
    BarTermination,
    Contamination,
    CoverageStatus,
    DayBoundarySpec,
    DensityTarget,
    EventClass,
    EventSpec,
    GroupCloseReason,
    Lookback,
    Measure,
    MeasureRole,
    Param,
    ParamRole,
    Relation,
    Shape,
    Side,
)

_ID, _VAR = MeasureRole.IDENTITY, MeasureRole.CANDIDATE_VARIABLE

_SIDE = Measure("side", "enum", "枚举", "本方方向，取自委托或成交的买卖标志，不做推断", _ID, enum_type=Side)
_PRICE = Measure("price", "price", "元 × 10000", "该结构所在价位，定点整数（core.raw.PRICE_SCALE 同源）", _ID)

TOTAL_ORDER: tuple[str, ...] = ("time_ms", "source_row_ord")
"""全部条目共用的排序键：交易所时刻，同毫秒按源文件行序。

**为什么不用成交编号**：`trades.seq` 在缺陷账本里有两条登记——7 天整列空（`seq_empty`）、
10 天稀疏重复（`seq_sparse_dup`）。拿一个已知不可靠的列当全序，等于把不可复现藏起来。
源行序在单机单趟读取下是稳定的，且 `core.book.depth_deltas` 已按它排；
它的弱点（跨文件块并行时不稳）由「内层按标的并行、一个标的一天不拆块」这条调度约束兜住。
"""

_CONTINUOUS = CONTINUOUS_EXCL_AUCTIONS
"""连续竞价两段，取自 `core.raw`，不另写一份时段常量。开盘集合竞价被显式排除。"""

_REDTEAM = "docs/design-log/2026-09-02-红队-新结构-{方法论,架构,工程}.md"
"""对照裁决出处：三个异构 agent（MiniMax / Gemini / OpenAI）在各自独立上下文里按各自判据
审过这四条，三方均判「需改 / 不得合并」，处置清单见 2026-09-03 交接文档第五节。
这不是「我们自己复核了一遍」——独立上下文、独立判据、结论与作者相左。

它们的简报内联了 CONTEXT.md，所以它们知道项目历史——**这对条目这一类对照是允许的**：
判断一条切割规则是不是过滤、会不会在集合竞价爆量，必须懂业务上下文才审得动。
要求盲的是对**假设**的对照裁决，那是另一类。见 CONTEXT.md「对照裁决」条。"""


LEVEL_BUILD_THEN_VANISH = EventSpec(
    kind="LevelBuildThenVanish",
    alias="假墙",
    event_class=EventClass.STRUCTURAL_EVENT,
    shape=Shape.INTERVAL,
    lookback=Lookback.INTRA_DAY,
    streams=("orders", "trades", "xinqing"),
    windows=_CONTINUOUS,
    total_order=TOTAL_ORDER,
    relation=Relation(
        roles=("建墙的本方委托", "拆墙的本方撤单", "该档位存续期间的成交", "峰值时刻的十档快照"),
        invariants=(
            Inv.RETURNS_TO_ZERO,
            Inv.NO_TRADES_DURING_LIFE,
            Inv.VISIBLE_IN_QUOTED_DEPTH_AT_PEAK,
            Inv.BUILT_BY_MULTIPLE_ORDERS,
        ),
        doc=(
            "同一 (标的, side, price) 上：一批委托把该档位从无堆到有，另一批撤单把它清回零，"
            "存续期间该档位一笔没成交，且**峰值时刻它在交易所发布的十档之内**。"
            "第三个角色为空集是分界线——有成交的档位消失是被吃掉的，不是被撤走的。"
            "第四个角色是 2026-09-03 实测加上去的：不在十档内的段占候选的 87%，"
            "形态完全不同（中位 9 笔委托堆起 5 千股，而十档内是 40 余笔堆起 2–7 万股），"
            "**看不见的墙吓不到人**"
        ),
    ),
    open_rule=(
        "某 (side, price) 档位被本方委托从无堆到有，且在峰值时刻落在十档之内。"
        "十档是**交易所定的发布范围**，不是我们选的一个数——它是存在性约束，不是幅度阈值"
    ),
    close_rule=(
        "该档位深度回到零。**墙没了这件事本身是结构终点**，不是「隔了多久」——"
        "庄家不掐表，是子弹打光了或者量到了。"
        "深度由 orders / trades 全量重建，**不看十档快照有没有把它挤出去**："
        "价格单边移动会让挂单跌出十档但它仍在撮合队列里，那不是消失（红队 2026-09-03 架构建议 8）"
    ),
    measures=(
        _SIDE,
        _PRICE,
        Measure("peak_vol", "int", "股", "该档位存续期间达到的最大展示深度。多大算大是因子层的事", _VAR),
        Measure("n_orders", "int", "笔", "把这个档位堆起来用了几笔委托。不变量要求至少两笔", _VAR),
        Measure("n_cancels", "int", "笔", "把它拆掉用了几笔撤单", _VAR),
        Measure("build_ms", "int", "毫秒", "从档位出现到达到 peak_vol 的毫秒", _VAR),
        Measure("life_ms", "int", "毫秒", "从档位出现到它消失的毫秒。不设上界——「几秒内撤才算假墙」是判断", _VAR),
        Measure("teardown_ms", "int", "毫秒", "从 peak_vol 到档位消失的毫秒。拆得比堆得快是几何事实，不是判定", _VAR),
        Measure(
            "level_at_peak", "int", "档",
            "峰值时刻该档位在十档里的第几档（1 = 本方最优价）。不变量已要求它非空，"
            "这里记下具体是第几档：实测 peak_vol 随档位加深单调上升，那是一个待解释的事实",
            _VAR,
        ),
        Measure(
            "ticks_from_touch_at_nearest_frame", "int", "最小价位变动",
            "峰值**前最近一帧**快照上，该档位离本方最优价几个 tick。"
            "名字如实说出测的是什么：快照 3 秒一帧，这不是瞬时值（红队 2026-09-03 方法论建议 12）",
            _VAR,
        ),
        Measure(
            "frame_age_ms", "int", "毫秒",
            "峰值时刻与所用那一帧快照之间差了多少毫秒。**对齐误差本身进事件流**，"
            "让下游看得见上一条度量有多近似，而不是假装它精确",
            _ID,
        ),
    ),
    params=(),
    contamination=Contamination.KNOWS_VERDICT,
    v1_audit=(
        "V1 §F：撤单残差为真但稀有闸不显著。不继承那个闸——V1 把「够不够稀有」焊死在提取期。"
        "本条目的稀有性来自结构本身（建起来又整个消失、全程零成交、峰值时可见），不来自任何闸"
    ),
    density_target=DensityTarget(
        max_rows_per_symbol_day=50.0,
        min_collapse_ratio=1000.0,
        basis=(
            "上界 50 = REGISTRY_ROW_BUDGET(100) 的一半：单条的上界只防**一条吃掉半个预算**，"
            "真正的成本约束是整张表的合计，由 admit_registry() 算。"
            "坍缩下界 1000 来自立项成本口径「5300 万行/天坍缩成几万条/天」≈ 1000 倍。"
            "**只设上界不设下界**：比目标更稀有不是缺陷，是更强的结构；"
            "「够不够做统计」是因子层的功效问题，不在这里判"
        ),
    ),
    contrast_verdict_ref=_REDTEAM,
)
"""区间 · 日内 · orders + trades + xinqing。**墙不是一笔委托，是一个档位，而且是看得见的档位。**

需要日内型是因为盘口深度要从开盘逐笔累积重建；零成交靠 trades 确认；可见性靠 xinqing 十档确认。
一笔孤立的撤单不构成事件：绝大多数撤单只让档位变薄，不会让它消失。"""


REFILL_AFTER_FILL = EventSpec(
    kind="RefillAfterFill",
    alias="冰山",
    event_class=EventClass.STRUCTURAL_EVENT,
    shape=Shape.GROUP,
    lookback=Lookback.INTRA_DAY,
    streams=("orders", "trades"),
    windows=_CONTINUOUS,
    total_order=TOTAL_ORDER,
    relation=Relation(
        roles=("把档位堆起来的那一笔委托", "把它整个吃光的成交", "同价同量把档位重新堆起来的下一笔委托"),
        invariants=(Inv.EVERY_CYCLE_ONE_ORDER_FULLY_EATEN, Inv.REFILL_STRICTLY_AFTER_FILL,
                    Inv.SLICE_EXCEEDS_MIN_LOT),
        doc=(
            "同一 (标的, side, price) 上，档位反复**被一笔委托堆起来 → 被成交整个吃光 → 回到零**，"
            "且每一轮的幅度相同、每片超过一手。"
            "**「档位归零」这一条替代了账户身份**：冰山的机制是同一个人吃完一片补一片，"
            "而逐笔数据里没有身份；要求归零，中间就没有别人，补上来的只能是同一个人。"
            "上一版拿「同价 + 同量」当身份——张三挂 300 被吃掉、李四十秒后也挂 300 就算一轮，"
            "两个毫不相干的人，于是数出 55–219 条/(标的·日)。"
            "代价是**只认得独占档位的切片**：热门价位上多人共存时，冰山与普通排队在数据上"
            "本来就分不开——分不开就不认"
        ),
    ),
    open_rule=(
        "某 (side, price) 档位被**一笔**超过一手的委托从零堆起、被成交**整个**吃光回到零之后，"
        "同价同量的下一笔委托又把它堆起来——即第一次完成「吃光 → 补片」。"
        "一轮即成立：「重复」的词义下限是 2 个循环，不是阈值。"
        "一手取 `core.raw.LOT_SIZE`：它是**交易所定的最小委托单位**，与十档发布范围同类，"
        "不是我们从分布里挑出来的一个数"
    ),
    close_rule=(
        "该档位下一个循环的幅度与本组不同（换了片的大小），或该档位不再回到零，"
        "或**撞上交易所的状态边界**（午休 · 收盘集合竞价开始 · 停牌 · 该标的当日结束）。"
        "**不得写「隔了 T 久没补就结组」**：状态边界是交易所定的结构，不是我们掐的表"
        "（红队 2026-09-03 工程严重 2）。代价是有人整天在同一价位补同样的量时这一组横跨半天——"
        "那本来就是事实，由因子层看 n_refills 与 span_ms 自己判断"
    ),
    measures=(
        _SIDE,
        _PRICE,
        Measure("slice_vol", "int", "股",
                "每片的委托量，等于每个循环的峰值深度。本组内恒定——它是成组的条件之一。"
                "不变量已要求它超过一手", _VAR),
        Measure("n_refills", "int", "次", "完成了几轮「吃光 → 补片」。几轮算多是判断，这里只记几轮", _VAR),
        Measure("total_filled", "int", "股", "本组累计被成交的量。它是 n_refills × slice_vol 的实测校验", _VAR),
        Measure("span_ms", "int", "毫秒", "首片到末片的毫秒跨度。组不是区间：两片之间该档位是空的", _VAR),
        Measure(
            "close_reason", "enum", "枚举",
            "本组因为什么结束：下一个循环换了幅度 / 该档位不再回到零 / 撞上状态边界。"
            "结组原因是结构信息，混在一起就分不清「补完了」与「被打断了」",
            _ID, enum_type=GroupCloseReason,
        ),
    ),
    params=(),
    contamination=Contamination.KNOWS_VERDICT,
    v1_audit=(
        "V1 §C：REP_MIN=10 是错代理，96% 的触发来自它。不继承——把 10 改成 3 只是把同一段判断挪个位置。"
        "本条目不设 k，改用「成交后才补」这个机制约束把稀有性做进结构里，n_refills 交给因子层裁"
    ),
    density_target=DensityTarget(
        max_rows_per_symbol_day=50.0,
        min_collapse_ratio=1000.0,
        basis="与假墙同一口径：预算的一半防独吞，合计由 admit_registry() 算。尚未实测",
    ),
    contrast_verdict_ref=_REDTEAM,
)
"""组 · 日内 · orders + trades。**冰山不是「连着报两笔一样的」，是「成交一片、补一片」。**

需要 trades 流才能知道前一片是不是被吃完了——这正是上一版单读 orders 就写得出来的原因：
它根本没看成交，所以它切的不是冰山。

**它与假墙用同一台机器**（`core.book.level_episodes`），只是互补地用：
假墙是「堆起来又整个消失、全程零成交」，冰山是「堆起来又整个消失、全部由成交消失、反复同一幅度」。
所以它**不需要成交与委托的关联**——档位重建只看深度。
（上一版需要，并因此记错过一个结论：「上交所关联率 57%」量的是混了主被动的聚合，
按被动方量两个市场都是 99.9%。见 design-log 冰山那篇。）"""


FILL_EXCEEDS_DISPLAYED = EventSpec(
    kind="FillExceedsDisplayed",
    alias="隐藏深度",
    event_class=EventClass.STRUCTURAL_EVENT,
    shape=Shape.INSTANT,
    lookback=Lookback.INTRA_DAY,
    streams=("xinqing", "trades", "orders"),
    windows=_CONTINUOUS,
    total_order=TOTAL_ORDER,
    relation=Relation(
        roles=(
            "前一帧快照在该档的展示量",
            "两帧之间在该档的成交",
            "两帧之间在该档新增的委托",
            "后一帧快照上该档仍然存在",
        ),
        invariants=(
            Inv.FILL_REACHES_DISPLAYED,
            Inv.LEVEL_SURVIVES_NEXT_FRAME,
            Inv.NO_NEW_ORDERS_BETWEEN_FRAMES,
        ),
        doc=(
            "成交量 ≥ 前一帧的展示量，该档位在后一帧仍然活着，**且两帧之间该档没有新增委托**。"
            "前两条是「幸存即隐藏」；第三条是它的必要补丁：3 秒一帧的间隔里，价格可以穿过该档"
            "再被新挂单重新撑住，那时后一帧看到的「活着」是新来的人，不是暗单"
            "（红队 2026-09-03 架构严重 4 / 方法论建议 11）。"
            "**加上第三个角色就必须加 orders 流**——单 xinqing + trades 物理上判不出来"
        ),
    ),
    open_rule=(
        "相邻两帧之间该档成交量 ≥ 前一帧该档展示量，该档位在后一帧仍存在于盘口，"
        "且两帧之间该 (side, price) 上的新增委托量为零"
    ),
    close_rule="同一时刻即结束：时刻型，start_time == end_time，没有跨度",
    measures=(
        _SIDE,
        _PRICE,
        Measure("displayed_vol", "int", "股", "前一帧在该档位的展示量", _VAR),
        Measure("executed_vol", "int", "股", "两帧之间在该档位的成交量", _VAR),
        Measure("surviving_vol", "int", "股", "后一帧该档位仍显示的量——它是「幸存」这件事的数值证据", _VAR),
        Measure(
            "added_vol", "int", "股",
            "两帧之间该档位的新增委托量。不变量要求它为零；记下来是为了让「零」这件事可核，"
            "而不是只在代码里判一下就丢掉",
            _ID,
        ),
        Measure("frame_gap_ms", "int", "毫秒", "两帧之间隔了多少毫秒。标称 3 秒，实际由数据说了算", _ID),
    ),
    params=(),
    contamination=Contamination.KNOWS_VERDICT,
    v1_audit=(
        "V1 §D 承接：隐藏深度的结构本身没被证伪，被证伪的是它的方向性解读。"
        "本条目只记几个量，方向留给因子层"
    ),
    density_target=DensityTarget(
        max_rows_per_symbol_day=50.0,
        min_collapse_ratio=1000.0,
        basis=(
            "与假墙同一口径：预算的一半防独吞，合计由 admit_registry() 算。"
            "2026-09-03 实测 32.68 条/(标的·日) 撞上了原来那个 30 的上界，"
            "用户裁定动上界——动的是**上界的出处**：原来的 10–30 没有推导过程"
            "（红队方法论致命 1），新的口径从常驻预算 ÷ 每行字节推出来，两个输入都标了是估算"
        ),
    ),
    contrast_verdict_ref=_REDTEAM,
)
"""时刻 · 日内 · xinqing + trades + orders。第一批里 D013（供应商在 09:30 / 13:00 每股各发两帧
重复开盘快照）影响最大的一条：两帧比对撞上重复帧会算出零成交量。归一在形状消歧层做，
注册表不知道 D013 存在。

⚠️ 未实测。"""


VOL_CLOCK_BAR = EventSpec(
    kind="VolClockBar",
    alias="成交量时钟",
    event_class=EventClass.BAR,
    shape=Shape.INTERVAL,
    lookback=Lookback.CROSS_DAY,
    streams=("trades",),
    windows=_CONTINUOUS,
    total_order=TOTAL_ORDER,
    relation=Relation(
        roles=("上一根 bar 结束后的第一笔成交", "本 bar 内的全部成交", "使累计额跨过刻度的那一笔"),
        invariants=(Inv.VOLUME_CROSSES_TICK,),
        doc="一根 bar 由一段连续成交构成，其累计成交额首次跨过 tick_amount 的那一笔即为终点",
    ),
    open_rule="上一根 bar 结束的那一笔成交之后的第一笔成交",
    close_rule=(
        "本 bar 累计成交额首次跨越刻度 tick_amount。**量到了就结**，不是时间到了就结——"
        "这正是「庄家不掐表」那条规矩在切割规则上的样子。"
        "bar 状态按 (标的, 交易日) 重置，当日末根不足刻度即为 partial"
    ),
    measures=(
        Measure("bar_index", "int", "序号", "当日第几根 bar，从 0 起", _ID),
        Measure("amount", "int", "元 × 10000", "本 bar 累计成交额。≥ tick_amount，末根可能不足（当日收盘截断）", _VAR),
        Measure("volume", "int", "股", "本 bar 累计成交量。它与 amount 的比值就是本 bar 的均价", _VAR),
        Measure("vwap", "price", "元 × 10000", "本 bar 的成交量加权均价，定点整数", _VAR),
        Measure("n_trades", "int", "笔", "本 bar 内的成交笔数", _VAR),
        Measure("termination", "enum", "枚举", "本根因为什么结束：跨过刻度 / 当日收盘截断。"
                "末根与整根不是一种东西，合起来统计会把收盘截断当成市场行为", _ID,
                enum_type=BarTermination),
    ),
    params=(
        Param(
            name="bars_per_day",
            role=ParamRole.SAMPLING_RESOLUTION,
            value=48,
            unit="根 / 交易日",
            why=(
                "刻度 tick_amount = daily_ref.amount 的 lookback_days 日均值 ÷ bars_per_day。"
                "上一版把刻度写成一句描述「daily_ref.volume 的 lookback_days 日均值」——照它跑一天只有"
                "一根 bar，那不是成交量时钟，而 28 个测试全绿。除数是采样分辨率：调它只让 bar 更粗或更细，"
                "不会把 bar 换成另一种东西（单调同伦）；不含任何关于「该量在样本中的排名」的判断（维度切分）；"
                "怎么取都至少切得出一根（不可调出空集）。48 对应连续竞价 4 小时里平均每 5 分钟一根。"
                "**改它是改设计，不是校准**：任何改动必须同时改 REGISTRY_VERSION，摘要会变。"
                "日均值取自 T−1 及以前，**绝不含当日自身**——否则 09:35 的 bar 边界会取决于 14:55"
            ),
            source="daily_ref.amount",
        ),
    ),
    contamination=Contamination.KNOWS_VERDICT,
    v1_audit=(
        "V1 accum_dist 的 bar 承接。V1 的 bar 与阈值焊在同一段代码里（阈值算在生成期），"
        "本条目只产 bar 与其几何，不产任何判定"
    ),
    contrast_verdict_ref=_REDTEAM,
    lookback_days=20,
    daily_ref_columns=("amount",),
)
"""区间 · 跨日 · trades + daily_ref。第一批里唯一的跨日条目——验证「注册表声明要什么、
驱动层决定怎么跑」这套分工。跨日不额外贵：外层按日期升序本来就是最快读法，T−1 在手边。

**刻度用成交额不用成交量**（红队 2026-09-03 架构严重 7）：高送转 / 配股当日的成交股数在同等
资金下天然翻倍，用 T−1 及以前未复权的成交量做刻度，会让当日切出近 100 根 bar，
稳态假设当场破掉。成交额不受送转影响。

**冷启动**：有效历史日不足 lookback_days 时**显式记缺口**（`Gap`），不产 bar，
严禁静默填 0 或临时缩短窗口。

它与另外三条不同：**不是「反常」，是一把尺子**——把活跃股与半死不活的股放到同一刻度上比。
`event_class=BAR`：条数由 bars_per_day 直接决定，不适用密度目标。"""


DAY_BOUNDARY = DayBoundarySpec(
    kind="DayBoundary",
    measures=(
        Measure(
            "coverage_status", "enum", "枚举",
            "该 (标的, 交易日) 有没有数据。**只答有没有，不答为什么**——归因是 gap_codes 的事",
            MeasureRole.IDENTITY, enum_type=CoverageStatus,
        ),
        Measure(
            "gap_codes", "enum", "缺陷账本 code",
            "缺口与形状的归因码，取值就是缺陷账本的 DefectCode（CI 校验二者相等）。"
            "「查不到 = 没有」被禁止：缺口必须携带归因",
            MeasureRole.IDENTITY, repeated=True, enum_type=DefectCode,
        ),
        Measure(
            "n_events", "int", "条",
            "当日该标的的事件条数（不含本条）。安静 = COVERED 且 n_events == 0，与缺口是两种信息。"
            "因子层的跨日累积从这里起步，不必回看原始层",
            MeasureRole.CANDIDATE_VARIABLE,
        ),
        Measure("quiet_span_ms", "int", "毫秒", "当日最长的无事件跨度。V1 唯一存活几何「持续安静」的原料",
                MeasureRole.CANDIDATE_VARIABLE),
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
