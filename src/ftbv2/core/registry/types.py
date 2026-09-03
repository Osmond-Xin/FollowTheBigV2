"""事件注册表接口上的类型。

**这个文件里哪些约束是真的、哪些只是让人写字，明确分开写。**
真约束 = 违反时构造失败且无法用「写一句话」绕过；
署名字段 = 强制作者写下理由，机器只查非空——它拦不住写错的人，只拦得住不写的人。
不要把后者当成门禁。上一版 commit 说「裁定 → raise，不是注释」，其中若干条其实就是注释，
2026-09-03 审计后改掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ftbv2.core.raw import STREAMS, DefectCode, Kind


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


@dataclass(frozen=True)
class Measure:
    """一条几何度量：事件记录的字段。**只记结构自身的几何**——方向、价、量、笔数、档位距离、
    时延、跨度。分位、排名、通过/不通过，以及这一段之外发生的事，一概不进事件流。

    `kind` 取 `core.raw.Kind`，不另立一份 dtype 词汇（上一版是自由字符串，
    与 `core.raw.output_dtype` 重复造轮子，2026-09-03 审计后改）。"""

    name: str
    kind: Kind
    unit: str
    doc: str
    repeated: bool = False
    enum_type: type[Enum] | None = None

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ValueError(f"几何度量名不是合法标识符：{self.name!r}")
        if not self.doc:
            raise ValueError(f"几何度量 {self.name} 缺 doc：字段语义属于接口，不得省")
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

    **这是本次审计的核心修正。** 上一版的 `QuoteThenWithdraw` 是「一笔委托被整笔撤销」——
    撤单标志就写在那一行上，不必看任何别的行。那是**过滤**（挑出满足某属性的单条记录），
    不是**提取**（找出多条记录之间的结构关系）。过滤不降维：每条事件仍对应一条原始行，
    等于把 orders 表挪了个地方。

    `roles` 至少两个：单行过滤只说得出一个角色。这是**设计期的气味检查，不是证明**——
    真正的把关是 `Density`：坍缩比要实测，1:1 会在数字上暴露，赖不掉。"""

    roles: tuple[str, ...]
    invariant: str

    def __post_init__(self) -> None:
        if len(self.roles) < 2:
            raise ValueError(
                f"结构关系只有 {len(self.roles)} 个角色：{self.roles}。"
                "一条事件必须由多条原始记录的关系构成——单行属性判断是过滤，不是提取，它不降维"
            )
        if not self.invariant:
            raise ValueError("结构关系缺 invariant：角色之间必须成立的关系属于接口（署名字段）")


@dataclass(frozen=True)
class Density:
    """一条条目的实测密度。**只能由实测填**，不接受估算。

    上一版有两个字段 `max_rows_per_symbol_day` / `expected_rows_per_symbol_day`，
    两个都是拍的，而校验只查 `0 < expected <= max`——两个虚构数之间的关系，真假都放行。
    2026-09-03 审计后删掉，改成这个：没测就是 None，`spec.density is None` 是一个诚实的「不知道」。"""

    rows_per_symbol_day: float
    collapse_ratio: float
    receipt: str

    def __post_init__(self) -> None:
        if self.rows_per_symbol_day <= 0:
            raise ValueError("实测条数必须为正")
        if self.collapse_ratio <= 1:
            raise ValueError(
                f"坍缩比 {self.collapse_ratio} ≤ 1：这条提取器没有降维。"
                "一条事件消耗的原始行不多于一行，就是把数据挪到了另一张表"
            )
        if not self.receipt:
            raise ValueError("实测密度必须带收据（ftbv2.io.receipt）：入库数字一律带血缘")


@dataclass(frozen=True)
class EventSpec:
    """事件注册表的一条条目。声明的是**要什么**，不是**怎么跑**——怎么跑由驱动层照着声明决定。

    **真约束**（违反即构造失败，写一句话绕不过去）：

    - `lookback is CYCLE` —— 周期型不产出事件行；
    - 跨日型缺 `lookback_days` / `daily_ref_columns`，或非跨日型带了它们；
    - `params` 里出现 decision_threshold，或参数的 value 不是数（由 Param 自己拒）；
    - `relation.roles` 少于两个（由 Relation 自己拒）——单行过滤不是提取；
    - `kind` 重名（表名冲突，在 registry 导入时就炸）；
    - `measures` 里 kind='enum' 而没给 enum_type（枚举取值只写在 docstring 里等于没有单源）。

    **署名字段**（机器只查非空，拦不住写错的人）：`alias` · `open_rule` · `close_rule` ·
    `v1_audit` · `relation.invariant` · `Param.why`。它们是给红队和人读的，不是门禁。

    **诚实的未知**：`density is None` 表示这条还没在真实数据上测过密度。
    `io.events` 全量提取前必须 `require_density()`——没测过的条目不许进全量扫描。
    """

    kind: str
    alias: str
    shape: Shape
    lookback: Lookback
    streams: tuple[str, ...]
    relation: Relation
    open_rule: str
    close_rule: str
    measures: tuple[Measure, ...]
    params: tuple[Param, ...]
    contamination: Contamination
    v1_audit: str
    density: Density | None = None
    lookback_days: int | None = None
    daily_ref_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._check_naming()
        self._check_lookback()
        self._check_measures()
        if not self.v1_audit:
            raise ValueError(f"{self.kind} 缺 v1_audit（署名字段）：V1 失效模式审计是进注册表的前置条件")

    def require_density(self) -> Density:
        """取实测密度；没测过就拒绝。全量提取前必须过这一关——
        「预算是拍的」这件事必须在花掉 15 小时之前暴露，而不是之后。"""
        if self.density is None:
            raise ValueError(
                f"{self.kind} 还没在真实数据上测过密度。先在样本日上跑一趟拿到条数与坍缩比，"
                "带收据填进 density，再进全量提取"
            )
        return self.density

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


@dataclass(frozen=True)
class DayBoundarySpec:
    """日界事件的 schema。**不是注册表条目**——由驱动层从 交易日历 × 标的主表 × 摄取收据的叉积产生，
    没有切割规则、没有 stream、没有提取参数。放在注册表里只为事实单源：因子层要读它的字段。

    不变量：每个 (标的, 交易日) 恰好一条。「安静」（COVERED 且 n_events == 0）与
    「缺口」（ABSENT）是两种信息，不得合并。

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
