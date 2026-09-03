"""事件注册表接口上的类型。CONTEXT.md 第三节的定义在这里变成机械可检查的约束：
事件三型、回看范围四档、边界由结构触发、提取参数只能是拓扑量——违反即构造失败。

接口 = 调用者必须知道的一切：不变量与错误模式写在各类型的 docstring 与 __post_init__ 里。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ftbv2.core.raw import STREAMS


class Shape(Enum):
    """事件三型（CONTEXT.md「事件」）。三型的 end_time 含义不同，因子层不得一概当作「事情结束了」。"""

    INTERVAL = "interval"
    """区间：起止之间那个东西确实存在着。假墙——墙挂上到墙撤掉。"""
    GROUP = "group"
    """组：一批离散记录被圈成整体，时间上未必连着；起止只是这组的头和尾。冰山——同价同量的一串委托。"""
    INSTANT = "instant"
    """时刻：没有跨度，start_time == end_time。隐藏深度——某个快照时点上发现的事实。"""


class Lookback(Enum):
    """回看范围四档（CONTEXT.md「回看范围」）：提取器为了切出一段，最远要往回看到哪里。
    分界不在「看得远不远」，在**能不能事先写下一个常数**。驱动层照此调度，重跑代价随档递增。"""

    INTRA_EPISODE = "intra_episode"
    """段内：只看这段前后有界的几行。改了只重跑那一天。"""
    INTRA_DAY = "intra_day"
    """日内：要当天一整天在手；多流对齐属于这一档。改了只重跑那一天。"""
    CROSS_DAY = "cross_day"
    """跨日：要 T−1 及以前的日级参考层，且写得出一个具体天数。改了重跑那个窗口。"""
    CYCLE = "cycle"
    """周期：回看范围写不出一个数，由数据本身决定（可跨年）。改了必须从语料起点全量重跑。
    ⚠️ **这一档不产出事件行**——EventSpec 构造时硬拒。周期尺度的量落日级参考层的列：
    它没有起止、每天都有值，回答的是「现在处于什么处境」而不是「发生了什么」，**处境不是事件**。"""


class ParamRole(Enum):
    """提取参数的角色。只有前三种能进提取器；DECISION_THRESHOLD 属于预注册，进注册表即构造失败。"""

    STRUCTURAL_PARTITION = "structural_partition"
    """切割边界的拓扑量：时间上界、笔数边、价位连续性边、离最优价的离散刻度边。"""
    SAMPLING_RESOLUTION = "sampling_resolution"
    """采样刻度：调它只改变段的粗细，不改变段是什么。"""
    CANDIDATE_RECALL_GATE = "candidate_recall_gate"
    """召回闸：调紧会单调减少候选数。必须登记召回覆盖率实测（recall_evidence）。"""
    DECISION_THRESHOLD = "decision_threshold"
    """判断阈值。**禁止进入提取器**——它属于预注册，那一层改起来是秒级的。"""


class Contamination(Enum):
    """污染级别（CONTEXT.md「污染级别」）：来源在多大程度上已经知道 V1 对同一问题的结论。
    按**知道多少**分，不按**是否看过**分。**是参考值，不是判据**：必填、进证据指纹、可被审计，
    但不进入任何机械判定。"""

    UNAWARE = "unaware"
    """不知道 V1 碰过这个问题。"""
    KNOWS_PRIOR = "knows_prior"
    """知道 V1 做过，不知道结论的方向。"""
    KNOWS_VERDICT = "knows_verdict"
    """知道 V1 的结论。第一批种子的出处都是 V1 失效模式审计，全部是这一档。"""


@dataclass(frozen=True)
class Measure:
    """一条几何度量：事件记录的字段。**只记结构自身的几何**——方向、价、量、笔数、档位距离、
    时延、跨度。分位、排名、通过/不通过，以及这一段之外发生的事，一概不进事件流。"""

    name: str
    dtype: str
    unit: str
    doc: str

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ValueError(f"几何度量名不是合法标识符：{self.name!r}")
        if not self.doc:
            raise ValueError(f"几何度量 {self.name} 缺 doc：字段语义属于接口，不得省")


@dataclass(frozen=True)
class Param:
    """一个提取参数。三类参数各有唯一住所，这里只放提取参数（CONTEXT.md「提取参数」）。
    `why` 必须说清它为什么是拓扑量而不是判断——三条机械判据（维度切分 · 单调同伦 · 不可调出空集）
    靠人写、靠 review 查，但角色标签靠 __post_init__ 硬拒。"""

    name: str
    role: ParamRole
    value: int | float | str
    unit: str
    why: str
    recall_evidence: str | None = None

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ValueError(f"提取参数名不是合法标识符：{self.name!r}")
        if self.role is ParamRole.DECISION_THRESHOLD:
            raise ValueError(
                f"提取参数 {self.name} 的角色是 decision_threshold：判断阈值属于预注册，不得进事件注册表。"
                "写进提取器就被锁进一次 15 小时的全量重跑里"
            )
        if not self.why:
            raise ValueError(f"提取参数 {self.name} 缺 why：为什么它是拓扑量而不是判断，属于接口")
        if self.role is ParamRole.CANDIDATE_RECALL_GATE and not self.recall_evidence:
            raise ValueError(f"召回闸 {self.name} 未登记召回覆盖率实测（recall_evidence）")


@dataclass(frozen=True)
class EventSpec:
    """事件注册表的一条条目。声明的是**要什么**，不是**怎么跑**——怎么跑由驱动层照着声明决定。

    构造时硬拒的四件事，每一件对应 CONTEXT.md 里的一条裁定：

    - `lookback is CYCLE` —— 周期型不产出事件行，产出日级参考层的列（处境不是事件）；
    - 跨日型缺 `lookback_days` 或 `daily_ref_columns`，或非跨日型带了它们 —— 回看范围的分界是
      「能不能事先写下一个常数」，写得出就不是周期型，写不出就不是跨日型；
    - `params` 里出现 decision_threshold —— 由 Param 自己拒；
    - `open_rule` / `close_rule` 为空 —— 边界由结构触发不由时间触发，触发条件属于接口，不得省。

    `kind` **按结构命名**，语义放 `alias`：`event_type == "spoof"` 这一列本身就是对下游的语义污染。
    `v1_audit` 写明该种子在 V1 的哪条失效模式上中过招、为何不继承那个否定。
    """

    kind: str
    alias: str
    shape: Shape
    lookback: Lookback
    streams: tuple[str, ...]
    open_rule: str
    close_rule: str
    measures: tuple[Measure, ...]
    params: tuple[Param, ...]
    contamination: Contamination
    v1_audit: str
    max_rows_per_symbol_day: int
    expected_rows_per_symbol_day: int
    lookback_days: int | None = None
    daily_ref_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._check_naming()
        self._check_lookback()
        self._check_measures()
        if self.max_rows_per_symbol_day <= 0:
            raise ValueError(f"{self.kind} 的 max_rows_per_symbol_day 必须为正：超预算硬失败、不发布")
        if not 0 < self.expected_rows_per_symbol_day <= self.max_rows_per_symbol_day:
            raise ValueError(
                f"{self.kind} 的期望条数 {self.expected_rows_per_symbol_day} 不在 (0, "
                f"{self.max_rows_per_symbol_day}] 内。信号稀少是设计目标：条目必须声明期望条数"
            )
        if not self.v1_audit:
            raise ValueError(f"{self.kind} 缺 v1_audit：V1 失效模式审计是进注册表的前置条件")

    def _check_naming(self) -> None:
        if not self.kind.isidentifier():
            raise ValueError(f"事件类型名不是合法标识符：{self.kind!r}")
        if not self.alias:
            raise ValueError(f"{self.kind} 缺 alias：结构名之外的语义别名单列，便于人读，下游不得消费")
        if not self.open_rule or not self.close_rule:
            raise ValueError(
                f"{self.kind} 缺开段或结段规则。边界由结构触发不由时间触发——"
                "庄家不掐表，是子弹打光了或者量到了；触发条件属于接口，不得省"
            )
        unknown = tuple(s for s in self.streams if s not in STREAMS)
        if not self.streams or unknown:
            raise ValueError(f"{self.kind} 的 streams 为空或含未登记流：{unknown or '(空)'}，已登记 {STREAMS}")

    def _check_lookback(self) -> None:
        if self.lookback is Lookback.CYCLE:
            raise ValueError(
                f"{self.kind} 声明了周期型回看范围：**周期型不产出事件行**。周期尺度的量没有起止、"
                "每天都有值，回答的是「现在处于什么处境」而不是「发生了什么」——处境不是事件，"
                "落日级参考层的列。若落成事件行，一个周期就是一条横跨数年的事件，"
                "起止要等周期走完才知道，提取器就在等未来，单向流式破掉"
            )
        cross_day = self.lookback is Lookback.CROSS_DAY
        if cross_day and not (self.lookback_days and self.lookback_days > 0):
            raise ValueError(f"{self.kind} 是跨日型却没写出 lookback_days：写不出一个数的不是跨日型")
        if cross_day and not self.daily_ref_columns:
            raise ValueError(f"{self.kind} 是跨日型却没声明读哪些日级参考层列")
        if not cross_day and (self.lookback_days is not None or self.daily_ref_columns):
            raise ValueError(
                f"{self.kind} 不是跨日型却带了 lookback_days / daily_ref_columns："
                "只有跨日型读 T−1 及以前的日级参考层"
            )

    def _check_measures(self) -> None:
        if not self.measures:
            raise ValueError(f"{self.kind} 没有几何度量：事件的全部信息就在这些字段里")
        names = [m.name for m in self.measures]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.kind} 的几何度量重名：{sorted(n for n in names if names.count(n) > 1)}")


@dataclass(frozen=True)
class DayBoundarySpec:
    """日界事件的 schema。**不是注册表条目**——它由驱动层从 交易日历 × 标的主表 × 摄取收据
    的叉积产生，没有切割规则、没有 stream、没有提取参数。放在注册表里只为事实单源：
    因子层要读它，schema 只能有一处定义。

    不变量：每个 (标的, 交易日) 恰好一条。没有任何结构的安静日也让因子状态机前进一步；
    「安静」（有数据、无结构）与「缺口」（数据不存在）是两种信息，不得合并。"""

    kind: str
    measures: tuple[Measure, ...]

    def __post_init__(self) -> None:
        if not self.measures:
            raise ValueError("日界事件没有字段：覆盖状态与缺口的归因码编码在它的属性里")
