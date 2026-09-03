"""事件注册表接口上的类型。

**这个文件里哪些约束是真的、哪些只是让人写字，明确分开写。**
真约束 = 违反时构造失败且无法用「写一句话」绕过；
署名字段 = 强制作者写下理由，机器只查非空——它拦不住写错的人，只拦得住不写的人。
不要把后者当成门禁。上一版 commit 说「裁定 → raise，不是注释」，其中若干条其实就是注释，
2026-09-03 审计后改掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ftbv2.core.raw import CONTINUOUS_EXCL_AUCTIONS, STREAMS, DefectCode, Kind, Window
from ftbv2.core.registry.predicates import InvariantCode, required_fields


class Shape(Enum):
    """事件三型（CONTEXT.md「事件」）。三型的 end_time 含义不同，因子层不得一概当作「事情结束了」。"""

    INTERVAL = "interval"
    """区间：起止之间那个东西确实存在着。假墙——档位被堆起来到它整个消失。"""
    GROUP = "group"
    """组：一批离散记录被圈成整体，时间上未必连着；起止只是这组的头和尾。冰山——成交与补单的交替序列。"""
    INSTANT = "instant"
    """时刻：没有跨度，start_time == end_time。隐藏深度——某个快照时点上发现的事实。"""


class Lookback(Enum):
    """回看范围四档（CONTEXT.md「回看范围」）：提取器为了切出一段，最远要往回看到哪里。
    分界不在「看得远不远」，在**能不能事先写下一个常数**。驱动层照此调度，重跑代价随档递增。"""

    INTRA_EPISODE = "intra_episode"
    """段内：只看这段前后有界的几行。改了只重跑那一天。"""
    INTRA_DAY = "intra_day"
    """日内：要当天一整天在手（含从开盘累积的盘口状态、多流对齐）。改了只重跑那一天。"""
    CROSS_DAY = "cross_day"
    """跨日：要 T−1 及以前的日级参考层，且写得出一个具体天数。改了重跑那个窗口。"""
    CYCLE = "cycle"
    """周期：回看范围写不出一个数，由数据本身决定（可跨年）。改了必须从语料起点全量重跑。
    ⚠️ **这一档不产出事件行**——EventSpec 构造时硬拒，周期尺度的量落日级参考层的列。"""


class EventClass(Enum):
    """条目是哪一类东西。**密度目标只对结构事件成立**——上一版把成交量时钟与假墙放进同一个
    密度门禁，而前者一天固定 48 根是设计出来的，不是稀有性（红队 2026-09-03 工程建议 2）。"""

    STRUCTURAL_EVENT = "structural_event"
    """稀有结构：它出现几次由市场决定，不由我们的参数决定。**必须声明密度目标**。"""
    BAR = "bar"
    """尺子：条数由采样分辨率直接决定，谈稀有性没有意义。**不得声明密度目标**。"""
    REFERENCE = "reference"
    """参考层：每个 (标的, 交易日) 一行的列，不是事件。**不得声明密度目标**。"""


class MeasureRole(Enum):
    """这个字段是干什么用的。**没有用途的字段就是死字段**——上一版四条 × 约 9 个几何度量
    没有任何地方说明它们将来被谁消费，等于给因子层预备了一个没人负责的组合空间
    （红队 2026-09-03 方法论严重 5）。"""

    IDENTITY = "identity"
    """定位这条事件是哪一条：方向、价位、序号、跨度。不进因子的备选变量集。"""
    CANDIDATE_VARIABLE = "candidate_variable"
    """未来因子的备选变量。**它们的总数就是多重检验作用域的下界**，
    由 `candidate_variables()` 数出来、由契约测试盯住：加一个字段就得改那个数。"""


class ParamRole(Enum):
    """提取参数的角色。只有前三种能进提取器；DECISION_THRESHOLD 进注册表即构造失败。"""

    STRUCTURAL_PARTITION = "structural_partition"
    """切割边界的拓扑量：笔数边、价位连续性边、离最优价的离散刻度边。"""
    SAMPLING_RESOLUTION = "sampling_resolution"
    """采样刻度：调它只改变段的粗细，不改变段是什么。"""
    CANDIDATE_RECALL_GATE = "candidate_recall_gate"
    """召回闸：调紧会单调减少候选数。必须登记召回覆盖率实测（recall_evidence）。"""
    DECISION_THRESHOLD = "decision_threshold"
    """判断阈值。**禁止进入提取器**——它属于预注册，那一层改起来是秒级的。"""


class Contamination(Enum):
    """污染级别：来源在多大程度上已经知道 V1 对同一问题的结论。按「知道多少」分，不按「是否看过」分。
    **是参考值，不是判据**：必填、进证据指纹、可被审计，但不进入任何机械判定。"""

    UNAWARE = "unaware"
    KNOWS_PRIOR = "knows_prior"
    KNOWS_VERDICT = "knows_verdict"


class Side(Enum):
    """本方方向。取自委托或成交的买卖标志，不做推断。
    以前这个枚举只写在一句 docstring 里（「1 买 / −1 卖」），2026-09-03 审计后变成类型。"""

    BUY = 1
    SELL = -1


class GroupCloseReason(Enum):
    """组型事件因为什么结束。**结组原因是结构信息**：混在一起就分不清「补完了」与「被打断了」，
    而后者的 n_refills 是被截断的下界，不是实际轮数（红队 2026-09-03 工程严重 2）。"""

    PRICE_OR_SIZE_CHANGED = "price_or_size_changed"
    """下一个循环换了幅度（片的大小变了）——这一组自己走完了。"""
    PRICE_CROSSED = "price_crossed"
    """该档位不再回到零：被穿过、被撤走、或收盘时还挂着。它不是「补完了」。"""
    SESSION_BOUNDARY = "session_boundary"
    """撞上交易所的状态边界：午休 · 收盘集合竞价开始 · 停牌 · 该标的当日结束。
    **这不是时间阈值**，是交易所定的结构边界；本组的轮数因此是被截断的。"""


class BarTermination(Enum):
    """一根 bar 因为什么结束。末根与整根不是一种东西，合在一起统计会把收盘截断当成市场行为。"""

    TICK_CROSSED = "tick_crossed"
    """累计额跨过刻度——正常的一根。"""
    SESSION_END = "session_end"
    """当日收盘截断，不足刻度。"""


class CoverageStatus(Enum):
    """日界事件上的覆盖状态。**只答「有没有数据」，不答「为什么没有」**——
    归因是 gap_codes 的事，它复用缺陷账本的 DefectCode，不在这里重造一份原因枚举。
    「安静」（COVERED 且当日无事件）与「缺口」（ABSENT）是两种信息，不得合并。"""

    COVERED = "covered"
    """三流都有该 (标的, 交易日) 的数据。停牌心跳也是 COVERED——它有数据，
    只是形状特殊，由 gap_codes 带 DefectCode.QUOTE_ONLY 说明。"""
    PARTIAL = "partial"
    """单边缺失：部分 stream 无该标的数据。哪一个、为什么，看 gap_codes。"""
    ABSENT = "absent"
    """该 (标的, 交易日) 没有任何原始数据。必带 gap_codes。"""


_JUDGEMENT_RE = re.compile(r"rank|pct|percentile|score|pass|abnormal|threshold|分位|排名|阈值|异常")
"""事件流字段名里不许出现的判断词（红队 2026-09-03 工程建议 1）。
它拦的是「后来有人往几何度量里加 is_abnormal 而测试仍绿」，不是所有误用。"""


@dataclass(frozen=True)
class Measure:
    """一条几何度量：事件记录的字段。**只记结构自身的几何**——方向、价、量、笔数、档位距离、
    时延、跨度。分位、排名、通过/不通过，以及这一段之外发生的事，一概不进事件流。

    `kind` 取 `core.raw.Kind`，不另立一份 dtype 词汇（上一版是自由字符串，
    与 `core.raw.output_dtype` 重复造轮子，2026-09-03 审计后改）。

    `role` 分「定位用」与「因子备选变量」两类：后者的总数就是多重检验作用域的下界，
    由 `candidate_variables()` 数出来（红队 2026-09-03 方法论严重 5）。
    字段名带分位 / 排名 / 通过 / 阈值一律构造失败——判断不进事件流，这条从注释升级成约束
    （红队 2026-09-03 工程建议 1）。"""

    name: str
    kind: Kind
    unit: str
    doc: str
    role: MeasureRole
    repeated: bool = False
    enum_type: type[Enum] | None = None

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ValueError(f"几何度量名不是合法标识符：{self.name!r}")
        if not self.doc:
            raise ValueError(f"几何度量 {self.name} 缺 doc：字段语义属于接口，不得省")
        if _JUDGEMENT_RE.search(self.name):
            raise ValueError(
                f"几何度量 {self.name} 的名字里带判断（分位 / 排名 / 通过 / 阈值）。"
                "事件流只记结构自身的几何——判断属于因子层，两层不共用命名空间"
            )
        if (self.kind == "enum") != (self.enum_type is not None):
            raise ValueError(
                f"几何度量 {self.name}：kind='enum' 与 enum_type 必须同时给出。"
                "枚举取值写在 docstring 里而不是类型里，等于没有单源"
            )


@dataclass(frozen=True)
class Param:
    """一个提取参数。三类参数各有唯一住所，这里只放提取参数。

    `value` 必须是一个**可执行的数或表达式规格**，不是一句描述。上一版 VolClockBar 的刻度写成
    「daily_ref.volume 的 lookback_days 日均值」——那是一句话，照它跑出来一天一根 bar，
    而 28 个测试全绿。2026-09-03 审计后加 `divisor` 这类结构化字段，并由 __post_init__ 查数值型。

    ⚠️ `why` 是**署名字段**：机器只查非空。它拦不住写错的人。"""

    name: str
    role: ParamRole
    value: int | float
    unit: str
    why: str
    source: str | None = None
    recall_evidence: str | None = None

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ValueError(f"提取参数名不是合法标识符：{self.name!r}")
        if self.role is ParamRole.DECISION_THRESHOLD:
            raise ValueError(
                f"提取参数 {self.name} 的角色是 decision_threshold：判断阈值属于预注册，不得进事件注册表"
            )
        if not isinstance(self.value, int | float) or isinstance(self.value, bool):
            raise ValueError(
                f"提取参数 {self.name} 的 value 不是数：{self.value!r}。"
                "参数必须可执行——写成一句描述的参数，跑起来才发现它没有内容"
            )
        if not self.why:
            raise ValueError(f"提取参数 {self.name} 缺 why（署名字段）")
        if self.role is ParamRole.CANDIDATE_RECALL_GATE and not self.recall_evidence:
            raise ValueError(f"召回闸 {self.name} 未登记召回覆盖率实测（recall_evidence）")


@dataclass(frozen=True)
class Relation:
    """事件由哪些原始记录之间的什么关系构成。

    **这是 2026-09-03 第一次审计的核心修正。** 上一版的「一笔委托被整笔撤销」——撤单标志就写在
    那一行上，不必看任何别的行。那是**过滤**（挑出满足某属性的单条记录），不是**提取**
    （找出多条记录之间的结构关系）。过滤不降维：每条事件仍对应一条原始行。

    **第二次修正在 `invariants` 上。** 上一版这里是一个自由字符串 `invariant: str`，
    机器只查非空——写 `"TODO"` 也过，三路红队一致判为门面。现在它是一组
    `InvariantCode`，每条码在 `predicates.py` 里挂一段真的能跑的 polars 表达式与它所读的列，
    并有一组最小反例单测。`doc` 留下来给人读，但**判据在码上，不在 doc 上**。

    `roles` 至少两个：单行过滤只说得出一个角色。这是**设计期的气味检查，不是证明**——
    真正的把关是实测坍缩比：1:1 会在数字上暴露，赖不掉。
    """

    roles: tuple[str, ...]
    invariants: tuple[InvariantCode, ...]
    doc: str

    def __post_init__(self) -> None:
        if len(self.roles) < 2:
            raise ValueError(
                f"结构关系只有 {len(self.roles)} 个角色：{self.roles}。"
                "一条事件必须由多条原始记录的关系构成——单行属性判断是过滤，不是提取，它不降维"
            )
        if not self.invariants:
            raise ValueError(
                "结构关系没有不变量：一组空的不变量恒真。"
                "角色之间必须成立的关系要写成 InvariantCode，每条码挂一段可跑的谓词"
            )
        if len(set(self.invariants)) != len(self.invariants):
            raise ValueError(f"结构关系的不变量重复：{[c.value for c in self.invariants]}")
        if not self.doc:
            raise ValueError("结构关系缺 doc（署名字段）：不变量码之外，人要读得懂它在说什么")

    def required_columns(self) -> tuple[str, ...]:
        """全部不变量所需的列，去重排序。候选表缺任何一列，判据都不许求值。"""
        return tuple(sorted({f for c in self.invariants for f in required_fields(c)}))


@dataclass(frozen=True)
class DensityTarget:
    """一条结构事件的**目标**密度区间。注意它是目标，不是实测——实测不在纯核里。

    上一版这里是 `Density`（实测值）直接挂在 `EventSpec` 上。红队 2026-09-03 架构严重 5
    指出这是架构死锁：实测值要进静态源码，就得有人在跑完样本后**反向修改源码文件**，
    而 CONTEXT 第二节明文「代码保持只读，严禁运行时反向修改源码，写回会立刻改变 tree_sha，
    与收据机制死锁」。现在的分工是：

    - 纯核（这里）只声明**目标**，它是设计期写下的，改它就是改设计，`digest()` 会变；
    - 实测走 `DensityMeasurement`，由样本日上的一趟真实提取产出，**住在收据里**；
    - 驱动层读收据、调 `admit_full_extraction()` 比对，通过了才下发全量。

    **没有下界**。目标只管两件事：条数上界（成本）与坍缩比下界（降维）。
    「至少要多稀有」不是这里的事——它是统计功效问题，属于因子层的预注册。
    上一版把「10–30 条/(标的·日)」当成两侧闸，会把实测 3.68 条的结构判成不合格，
    而 3.68 条 × 约 3000 标的 × 1122 天 = 千万量级事件，功效绰绰有余。

    ⚠️ **单条的上界不是真正的约束，整张表的合计才是**（2026-09-03 用户裁定后重新推导）。
    常驻预算是整个事件层共用的一个数，一条条目占多少要看别人占了多少。
    所以这里的上界只防「一条吃掉半个预算」，真正的钱袋子由
    `registry.admit_registry()` 按 `REGISTRY_ROW_BUDGET` 一起算。
    """

    max_rows_per_symbol_day: float
    min_collapse_ratio: float
    basis: str

    def __post_init__(self) -> None:
        if self.max_rows_per_symbol_day <= 0:
            raise ValueError("条数上界必须为正")
        if self.min_collapse_ratio <= 1:
            raise ValueError(
                f"坍缩比下界 {self.min_collapse_ratio} ≤ 1：一条事件消耗的原始行不多于一行，"
                "就是把数据挪到了另一张表。这个下界写成 1 等于没有下界"
            )
        if not self.basis:
            raise ValueError("密度目标缺 basis（署名字段）：这两个数从哪来必须写下来")


@dataclass(frozen=True)
class EvidenceRef:
    """一次实测的血缘。**结构化，不是一句 `receipt="measured"`。**

    红队 2026-09-03 工程严重 4：`receipt: str` 非空是门面——两个月后换供应商、改排序键、
    换日历，事件数变化无法定位。这里绑住的是：收据 id（内容寻址，`.lineage/receipts/<id>.json`）·
    输入清单摘要 · 提取器所在的 commit · 被测规格的摘要 · 排序键。
    """

    receipt_id: str
    input_manifest_sha256: str
    extractor_commit: str
    spec_digest: str
    sort_key: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _is_hex(self.receipt_id, 64):
            raise ValueError(f"收据 id 不是 64 位十六进制：{self.receipt_id!r}（内容寻址的收据文件名）")
        if not _is_hex(self.input_manifest_sha256, 64):
            raise ValueError(f"输入清单摘要不是 64 位十六进制：{self.input_manifest_sha256!r}")
        if not _is_hex(self.extractor_commit, 40):
            raise ValueError(f"提取器 commit 不是 40 位十六进制：{self.extractor_commit!r}")
        if not self.spec_digest:
            raise ValueError("缺被测规格的摘要：不知道量的是哪一版定义，这个数就没有意义")
        if not self.sort_key:
            raise ValueError(
                "缺排序键：同毫秒多笔在不同切分下会改变「谁先把档位堆到零」，"
                "不声明排序键的实测不可复现（红队 2026-09-03 工程严重 5）"
            )


@dataclass(frozen=True)
class DensityMeasurement:
    """一次真实提取量出来的密度。**不进 `EventSpec`**——它住在收据里，由驱动层读进来。

    `collapse_ratio` 的分母定义（红队 2026-09-03 方法论严重 4 ③要求写明）：
    **该条目声明的 streams 在该样本日、该样本宇宙上实际读入的行数之和**，
    不是全宇宙行数，也不是单流行数。分子是产出的事件条数。
    """

    kind: str
    rows_per_symbol_day: float
    collapse_ratio: float
    symbol_days: int
    input_rows: int
    event_rows: int
    evidence: EvidenceRef

    def __post_init__(self) -> None:
        if self.symbol_days <= 0 or self.input_rows <= 0:
            raise ValueError("实测必须有样本：标的·日数与读入行数都要为正")
        if self.event_rows < 0:
            raise ValueError("事件条数不能为负")


def _is_hex(value: str, width: int) -> bool:
    return len(value) == width and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class EventSpec:
    """事件注册表的一条条目。声明的是**要什么**，不是**怎么跑**——怎么跑由驱动层照着声明决定。

    **真约束**（违反即构造失败，写一句话绕不过去）：

    - `lookback is CYCLE` —— 周期型不产出事件行；
    - 跨日型缺 `lookback_days` / `daily_ref_columns`，或非跨日型带了它们；
    - `params` 里出现 decision_threshold，或参数的 value 不是数（由 Param 自己拒）；
    - `relation.roles` 少于两个、`relation.invariants` 为空（由 Relation 自己拒）；
    - `kind` 重名（表名冲突，在 registry 导入时就炸）；
    - 几何度量 kind='enum' 而没给 enum_type，或字段名里带判断词（由 Measure 自己拒）；
    - `event_class is STRUCTURAL_EVENT` 却没有 `density_target`，或非结构事件带了它；
    - `windows` 为空，或声明了开盘集合竞价而没写下 `auction_reason`；
    - `total_order` 为空——同毫秒不声明次序的提取器不可复现。

    **署名字段**（机器只查非空，拦不住写错的人）：`alias` · `open_rule` · `close_rule` ·
    `v1_audit` · `relation.doc` · `Param.why` · `DensityTarget.basis`。
    它们是给红队和人读的，不是门禁。

    **实测密度不在这里**（红队 2026-09-03 架构严重 5）：纯核只写目标区间；实测走
    `DensityMeasurement` + 收据，由 `registry.admit_full_extraction()` 在下发全量前比对。
    没有人需要把量出来的数字反向写回源码。
    """

    kind: str
    alias: str
    event_class: EventClass
    shape: Shape
    lookback: Lookback
    streams: tuple[str, ...]
    windows: tuple[Window, ...]
    total_order: tuple[str, ...]
    relation: Relation
    open_rule: str
    close_rule: str
    measures: tuple[Measure, ...]
    params: tuple[Param, ...]
    contamination: Contamination
    v1_audit: str
    density_target: DensityTarget | None = None
    contrast_verdict_ref: str | None = None
    auction_reason: str | None = None
    lookback_days: int | None = None
    daily_ref_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._check_naming()
        self._check_lookback()
        self._check_measures()
        self._check_class_and_windows()
        if not self.v1_audit:
            raise ValueError(f"{self.kind} 缺 v1_audit（署名字段）：V1 失效模式审计是进注册表的前置条件")

    def require_contrast(self) -> str:
        """取对照裁决出处；没有就拒绝。与密度同级的一道门禁——
        四条种子由同一人在同一上下文里写完、且全部 `KNOWS_VERDICT`，
        没有独立裁决就没有任何东西能拦住「V1 对什么结构有意义的判断」被整批继承
        （红队 2026-09-03 方法论致命 3）。"""
        if not self.contrast_verdict_ref:
            raise ValueError(
                f"{self.kind} 没有对照裁决：本条目由谁在独立上下文里按独立判据审过，必须写得出出处。"
                "盲验不可能被机械保证，能机械保证的只有「有没有安排对照」这一件事"
            )
        return self.contrast_verdict_ref

    def candidate_variables(self) -> tuple[str, ...]:
        """本条目给因子层留下的备选变量名。它们的总数进多重检验作用域。"""
        return tuple(m.name for m in self.measures if m.role is MeasureRole.CANDIDATE_VARIABLE)

    def _check_naming(self) -> None:
        if not self.kind.isidentifier():
            raise ValueError(f"事件类型名不是合法标识符：{self.kind!r}")
        if not self.alias:
            raise ValueError(f"{self.kind} 缺 alias（署名字段）：结构名之外的语义别名单列，下游不得消费")
        if not self.open_rule or not self.close_rule:
            raise ValueError(f"{self.kind} 缺开段或结段规则（署名字段）：触发条件属于接口，不得省")
        unknown = tuple(s for s in self.streams if s not in STREAMS)
        if not self.streams or unknown:
            raise ValueError(f"{self.kind} 的 streams 为空或含未登记流：{unknown or '(空)'}，已登记 {STREAMS}")

    def _check_lookback(self) -> None:
        if self.lookback is Lookback.CYCLE:
            raise ValueError(
                f"{self.kind} 声明了周期型回看范围：**周期型不产出事件行**。周期尺度的量没有起止、"
                "每天都有值，回答的是「现在处于什么处境」而不是「发生了什么」——处境不是事件"
            )
        cross_day = self.lookback is Lookback.CROSS_DAY
        if cross_day and not (self.lookback_days and self.lookback_days > 0):
            raise ValueError(f"{self.kind} 是跨日型却没写出 lookback_days：写不出一个数的不是跨日型")
        if cross_day and not self.daily_ref_columns:
            raise ValueError(f"{self.kind} 是跨日型却没声明读哪些日级参考层列")
        if not cross_day and (self.lookback_days is not None or self.daily_ref_columns):
            raise ValueError(f"{self.kind} 不是跨日型却带了 lookback_days / daily_ref_columns")

    def _check_measures(self) -> None:
        if not self.measures:
            raise ValueError(f"{self.kind} 没有几何度量：事件的全部信息就在这些字段里")
        names = [m.name for m in self.measures]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.kind} 的几何度量重名：{sorted(n for n in names if names.count(n) > 1)}")

    def _check_class_and_windows(self) -> None:
        structural = self.event_class is EventClass.STRUCTURAL_EVENT
        if structural and self.density_target is None:
            raise ValueError(
                f"{self.kind} 是结构事件却没有密度目标：它出现几次由市场决定，"
                "不写下成本上界与坍缩下界，就没有任何东西能在花掉 15 小时之前拦住爆量"
            )
        if not structural and self.density_target is not None:
            raise ValueError(
                f"{self.kind} 不是结构事件（{self.event_class.value}）却带了密度目标："
                "尺子与参考层的条数由采样分辨率直接决定，对它谈稀有性没有意义"
            )
        if not self.windows:
            raise ValueError(
                f"{self.kind} 没声明适用时段：集合竞价允许撤单且撮合未开始，"
                "不排除它，「建档、撤回零、期间零成交」会整批误报（红队 2026-09-03 三方一致）"
            )
        opening = min(w.start_ms for w in self.windows) < CONTINUOUS_EXCL_AUCTIONS[0].start_ms
        if opening and not self.auction_reason:
            raise ValueError(
                f"{self.kind} 的适用时段伸进了开盘集合竞价却没写 auction_reason："
                "要这一段是可以的，但它是另一个样本，预注册必须写明用的是哪个"
            )
        if not self.total_order:
            raise ValueError(
                f"{self.kind} 没声明排序键：同一毫秒的多笔在不同切分、不同线程下会改变"
                "「谁先耗尽、谁补单、哪笔跨过刻度」，结果不可复现（红队 2026-09-03 工程严重 5）"
            )


@dataclass(frozen=True)
class DayBoundarySpec:
    """日界事件的 schema。**不是注册表条目**——由驱动层从 交易日历 × 标的主表 × 摄取收据的叉积产生，
    没有切割规则、没有 stream、没有提取参数。放在注册表里只为事实单源：因子层要读它的字段。

    不变量：每个 (标的, 交易日) 恰好一条。「安静」（COVERED 且 n_events == 0）与
    「缺口」（ABSENT）是两种信息，不得合并。

    **停牌心跳日**（`DefectCode.QUOTE_ONLY`）由 `yields_events()` 短路：那天该标的没有委托也没有成交，
    任何读 orders / trades 的条目都不产事件行，由本条兜底让因子状态机照样前进一步
    （红队 2026-09-03 架构严重 6）。

    `gap_codes` 的取值就是 `core.raw.DefectCode`——上一版这里写的是 `list[str]` 加一句
    「与缺陷账本共用 DefectCode」，一行都没 import，事实单源是句口号。2026-09-03 审计后接回。"""

    kind: str
    measures: tuple[Measure, ...]

    def __post_init__(self) -> None:
        if not self.measures:
            raise ValueError("日界事件没有字段：覆盖状态与缺口的归因码编码在它的属性里")
        by_name = {m.name: m for m in self.measures}
        if by_name.get("coverage_status") is None or by_name["coverage_status"].enum_type is not CoverageStatus:
            raise ValueError("日界事件的 coverage_status 必须用 CoverageStatus 枚举，不是自由整数")
        gap = by_name.get("gap_codes")
        if gap is None or gap.enum_type is not DefectCode or not gap.repeated:
            raise ValueError(
                "日界事件的 gap_codes 必须是 DefectCode 的重复字段——归因码与缺陷账本单源，"
                "CI 已校验 DefectCode 等于账本 code 集合，这里不得另起一份"
            )


def yields_events(spec: EventSpec, coverage: CoverageStatus, gap_codes: tuple[DefectCode, ...]) -> bool:
    """这一天这个标的，该条目会不会产出事件行。

    **短路而不是死等**（红队 2026-09-03 架构严重 6）：停牌心跳日只有行情、没有委托没有成交，
    读 orders / trades 的提取器在「等成交到达」上会挂住；没有任何数据的天更是无从切起。
    两种情形都返回 False，由日界事件兜底——**安静与缺口是两种信息，都不是「跳过这一天」**。
    """
    if coverage is CoverageStatus.ABSENT:
        return False
    tick_streams = {"orders", "trades"}
    if DefectCode.QUOTE_ONLY in gap_codes and tick_streams & set(spec.streams):
        return False
    return True
