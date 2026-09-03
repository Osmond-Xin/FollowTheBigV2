"""密度回归：在样本日上按注册表的不变量切出候选，给出分布与实测密度。

**存在的理由**：切割规则不该由我们指定，要用数据回归出来（2026-09-03 用户裁定）。
「一堵墙必须到过最优价吗」是这套东西回答的第一个问题，答案是**不**——该切的一刀是可见性。

**一个入口，按条目派生候选。** 结构事件共用的部分——读三流 · 抽样 · 拒绝缺口 ·
用注册表的不变量算候选 · 汇成实测密度——只有一份；不同的只是「候选表怎么生成」，
由 `_BUILDERS` 按 kind 取。加一条新事件是加一个生成器，不是抄一遍这个文件。

**判据从注册表取，不在这里重写**：候选掩码 = `core.registry.holds(spec.relation.invariants, ...)`。
上一版把 `closed & executed_vol == 0` 在三个统计函数里各抄了一遍——判据有三份就等于没有单源。

**适用时段约束的是产出，不是读取**：条目声明连续竞价两段，但它们是**日内型**——
盘口深度必须从开盘逐笔累积，集合竞价里挂下、连续竞价里才撤的委托也在队列里。
所以三流照读全天，只把落在窗内的候选算作产出。
（2026-09-03 实测踩到：把窗下推到读取层，撤单关联不上的比例从 0.04% 跳到 1.31%——
被过滤掉的正是那些集合竞价里挂下的委托。缺口如实报了出来，才发现窗下错了层。）

逻辑全在 `core.book`（纯核，进 CI）；这里只负责读与编排——读用 `io.raw.RawStore`，
不自己碰 parquet；价与流的口径取 `core.raw`，不另立一份。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

import polars as pl

from ftbv2.core.book import (
    attach_touch,
    attach_visibility,
    depth_deltas,
    frame_levels,
    frame_transitions,
    level_episodes,
    quote_levels,
)
from ftbv2.core.raw import Day, DefectLedger, Gap, ReadRequest, Window, in_windows, plan
from ftbv2.core.registry import DensityMeasurement, EvidenceRef, holds, spec
from ftbv2.io.raw import RawStore

WALLS = "LevelBuildThenVanish"
HIDDEN = "FillExceedsDisplayed"

_NEEDED = {
    "orders": ("time_ms", "oid", "type", "side", "price", "vol"),
    "trades": ("time_ms", "code", "bs", "price", "vol", "ask_ref", "bid_ref"),
    "xinqing": ("time_ms", *[f"{s}_{k}_{i}" for s in ("ask", "bid") for k in ("px", "sz")
                             for i in range(1, 11)]),
}


@dataclass(frozen=True)
class Candidates:
    """一天一条条目的候选表 + 该条目自己的分布切片与附带计数。

    `frame` 必须带 `t_start` / `t_end`（适用时段由通用部分裁）与不变量所需的全部列。
    每条条目特有的看法留给它自己——假墙看可见档位，隐藏深度看三个角色各砍掉多少；
    通用部分不猜每条条目该怎么看自己。
    """

    frame: pl.DataFrame
    extra: dict[str, int]


@dataclass(frozen=True)
class DayProbe:
    """一天的回归结果。`extra` 里的 `unlinked_cancels` 是缺口，不是零——必须随分布一起看。"""

    day: Day
    n_symbols: int
    rows_read: dict[str, int]
    n_rows: int
    n_candidates: int
    seconds: float
    distributions: dict[str, list[dict[str, object]]]
    extra: dict[str, int]

    @property
    def input_rows(self) -> int:
        """坍缩比的分母：该条目声明的 streams 在本日本样本上实际读入的行数之和。"""
        return sum(self.rows_read.values())


@dataclass(frozen=True)
class Probe:
    """一批样本日的回归结果。

    **多天是常态，不是可选项**：一天一个样本回答不了「这条结构在不同行情下稳不稳」，
    而密度是要拿去决定花不花 15 小时的（红队 2026-09-03 方法论严重 4）。
    """

    kind: str
    days: tuple[Day, ...]
    per_day: tuple[DayProbe, ...]
    symbol_days: int
    input_rows: int
    n_rows: int
    n_candidates: int
    seconds: float
    rows_per_symbol_day: float
    collapse_ratio: float
    invariants: tuple[str, ...]

    def measurement(self, evidence: EvidenceRef) -> DensityMeasurement:
        """把这批实测封成注册表准入用的实测记录。数字进收据，不进源码。"""
        return DensityMeasurement(
            kind=self.kind,
            rows_per_symbol_day=self.rows_per_symbol_day,
            collapse_ratio=self.collapse_ratio,
            symbol_days=self.symbol_days,
            input_rows=self.input_rows,
            event_rows=self.n_candidates,
            evidence=evidence,
        )


def probe(store: RawStore, ledger: DefectLedger, kind: str, days: tuple[Day, ...],
          sample: int = 0, seed: int = 0) -> Probe:
    """在给定样本日上按条目的不变量数出候选，并给出分布与实测密度。

    不做任何**判断**（多大算大、多快算快），只做条目已经声明的**结构约束**。

    `sample > 0` 时每天随机抽这么多标的。**标的全集取自当日 orders 实际出现的标的**——
    不能用 row group 的 symbol_min / symbol_max 当全集，那只是每个 row group 的边界值，
    抽出来的样本会系统性偏向排序边界（2026-09-03 第一次跑就踩了这个）。
    """
    if kind not in _BUILDERS:
        raise KeyError(f"没有 {kind!r} 的候选生成器；已有：{', '.join(_BUILDERS)}")
    per_day = tuple(_probe_one_day(store, ledger, kind, d, sample, seed) for d in days)
    symbol_days = sum(p.n_symbols for p in per_day)
    input_rows = sum(p.input_rows for p in per_day)
    candidates = sum(p.n_candidates for p in per_day)
    return Probe(
        kind=kind,
        days=days,
        per_day=per_day,
        symbol_days=symbol_days,
        input_rows=input_rows,
        n_rows=sum(p.n_rows for p in per_day),
        n_candidates=candidates,
        seconds=round(sum(p.seconds for p in per_day), 1),
        rows_per_symbol_day=candidates / symbol_days,
        collapse_ratio=input_rows / candidates if candidates else float("inf"),
        invariants=tuple(c.value for c in spec(kind).relation.invariants),
    )


def _probe_one_day(store: RawStore, ledger: DefectLedger, kind: str, day: Day,
                   sample: int, seed: int) -> DayProbe:
    t0 = time.time()
    entry = spec(kind)
    frames, rows, gaps = {}, {}, []
    symbols: frozenset[str] | None = None
    for stream, names in _NEEDED.items():
        req = ReadRequest(stream, (day,), names, symbols)   # 窗不下推，见模块 docstring
        res = store.execute(plan(req, store.catalog(stream, (day,)), ledger))
        frames[stream], rows[stream] = res.frame, res.stats.rows
        gaps.extend(res.gaps)
        if stream == "orders" and sample:
            universe = sorted(frames["orders"]["symbol"].unique().to_list())
            symbols = frozenset(random.Random(seed).sample(universe, min(sample, len(universe))))
            frames["orders"] = frames["orders"].filter(pl.col("symbol").is_in(sorted(symbols)))
            rows["orders"] = frames["orders"].height
    _refuse_gaps(day, tuple(gaps))

    built = _BUILDERS[kind](frames)
    table = built.frame.with_columns(
        (holds(entry.relation.invariants, tuple(built.frame.columns))
         & _within(entry.windows)).alias("candidate"))
    return DayProbe(
        day=day,
        n_symbols=frames["orders"]["symbol"].n_unique(),
        rows_read=rows,
        n_rows=table.height,
        n_candidates=int(table["candidate"].sum()),
        seconds=round(time.time() - t0, 1),
        distributions=_summarise(kind, table),
        extra=built.extra,
    )


# ------------------------------------------------------------------ 候选生成器（每条条目一个）

def _walls(frames: dict[str, pl.DataFrame]) -> Candidates:
    """假墙：档位深度从 0 堆起来到回落至 0 的一段，接上峰值时刻的最优价距离与可见档位。"""
    delta = depth_deltas(frames["orders"], frames["trades"])
    quotes = frames["xinqing"]
    episodes = attach_touch(level_episodes(delta.deltas),
                            quotes.rename({"ask_px_1": "ask1", "bid_px_1": "bid1"}))
    episodes = attach_visibility(episodes, quote_levels(quotes))
    return Candidates(
        frame=episodes,
        extra={"unlinked_cancels": delta.unlinked_cancels, "total_cancels": delta.total_cancels,
               "n_closed": int(episodes.filter(pl.col("closed")).height)},
    )


def _hidden(frames: dict[str, pl.DataFrame]) -> Candidates:
    """隐藏深度：相邻两帧之间每个档位的 展示量 → 成交 → 新增委托 → 幸存量。

    时刻型没有跨度，`t_start == t_end == 前帧时刻`——适用时段按前帧那一刻裁。
    """
    delta = depth_deltas(frames["orders"], frames["trades"])
    moves = frame_transitions(frame_levels(frames["xinqing"]), delta.deltas)
    return Candidates(
        frame=moves.with_columns(pl.col("q_time").alias("t_start"),
                                 pl.col("q_time").alias("t_end")),
        extra={"unlinked_cancels": delta.unlinked_cancels, "total_cancels": delta.total_cancels},
    )


_BUILDERS: dict[str, Callable[[dict[str, pl.DataFrame]], Candidates]] = {
    WALLS: _walls,
    HIDDEN: _hidden,
}


# ------------------------------------------------------------------ 分布（只为看，不是切割规则）

def _summarise(kind: str, table: pl.DataFrame) -> dict[str, list[dict[str, object]]]:
    if kind == WALLS:
        return {"by_visible_level": _by_level(table).to_dicts(),
                "by_ticks": _by_ticks(table).to_dicts()}
    return {"by_role": _by_role(table).to_dicts()}


def _by_ticks(table: pl.DataFrame) -> pl.DataFrame:
    """按离最优价的 tick 数分桶：0（就在最优价）· 1–4 · 5–9 · ≥10 · 无快照可比。
    分桶只为看分布，不是切割规则——切割规则里出现桶边就是判断。"""
    col = pl.col("ticks_from_touch_at_nearest_frame")
    bucket = (
        pl.when(col.is_null()).then(pl.lit("无快照"))
        .when(col == 0).then(pl.lit("0 最优价"))
        .when(col < 5).then(pl.lit("1-4"))
        .when(col < 10).then(pl.lit("5-9"))
        .otherwise(pl.lit("10+"))
    )
    return (
        table.with_columns(bucket.alias("bucket")).group_by("bucket")
        .agg(pl.len().alias("段数"), pl.col("candidate").sum().alias("候选数"),
             pl.col("peak_vol").median().alias("peak_vol_中位"),
             pl.col("life_ms").median().alias("life_ms_中位"),
             pl.col("n_adds").median().alias("n_adds_中位"))
        .sort("段数", descending=True)
    )


def _by_level(table: pl.DataFrame) -> pl.DataFrame:
    """按峰值时刻该档位在十档里的第几档分组；−1 = 峰值那一帧它不在十档内（看不见）。"""
    return (
        table.group_by(pl.col("level").fill_null(-1).alias("可见档位"))
        .agg(pl.len().alias("段数"), pl.col("candidate").sum().alias("候选数"),
             pl.col("peak_vol").median().alias("peak_vol_中位"),
             pl.col("n_adds").median().alias("n_adds_中位"))
        .sort("可见档位")
    )


def _by_role(table: pl.DataFrame) -> pl.DataFrame:
    """三个角色逐条加上去，看每一条各自砍掉多少。

    **这是为了让「第三个角色到底管不管用」变成一个数**：红队说不加「两帧之间无新增委托」，
    3 秒穿档后的新挂单会被误判成暗单。那就量一量它砍掉了多少，别只是听着有道理。
    """
    fills = pl.col("executed_vol") >= pl.col("displayed_vol")
    survives = pl.col("surviving_vol") > 0
    quiet = pl.col("added_vol") == 0
    steps = [
        ("全部 帧×档位", pl.lit(True)),  # noqa: FBT003
        ("成交 ≥ 展示量", fills),
        ("· 且后帧仍在", fills & survives),
        ("· 且两帧间无新增委托", fills & survives & quiet),
        ("· 且落在适用时段内（= 候选）", pl.col("candidate")),
    ]
    return pl.DataFrame([
        {"口径": name, "行数": int(table.filter(mask).height),
         "displayed_vol_中位": table.filter(mask)["displayed_vol"].median(),
         "frame_gap_ms_中位": table.filter(mask)["frame_gap_ms"].median()}
        for name, mask in steps
    ])


# ------------------------------------------------------------------ 通用约束

def _within(windows: tuple[Window, ...]) -> pl.Expr:
    """整段生命周期都落在适用时段内。跨过午休或伸进集合竞价的段不算产出。
    用 `core.raw.in_windows`，不另写一份时段判断。"""
    return in_windows("t_start", windows) & in_windows("t_end", windows)


def _refuse_gaps(day: Day, gaps: tuple[Gap, ...]) -> None:
    """样本日缺流就拒绝出数，不静默出一份看起来正常的分布。

    2026-09-03 实测踩到：20220627 只摄取了 orders / trades，没有 xinqing。
    读取层如实报了 `DAY_MISSING`，而上一版的本函数把 `res.gaps` 整个丢掉，于是
    「没有快照可比」和「档位不在十档内」都变成 `level = null`，那天的可见档位候选数是 0——
    一个看起来像行情、其实是缺文件的数。**「查不到 = 没有」被禁止**，这里是它的具体形态。
    """
    if gaps:
        detail = sorted({f"{g.stream}:{g.reason.value}" for g in gaps})
        raise ValueError(
            f"{day} 的样本有缺口 {detail}，拒绝出密度：缺流会让「看不见」与「没数据」变成同一个 null。"
            "先把这一天补齐或把它排除出样本，不要拿这份分布当实测"
        )
