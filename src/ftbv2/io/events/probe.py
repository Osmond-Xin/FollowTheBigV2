"""假墙密度回归（`LevelBuildThenVanish` 的候选生成 + 分布 + 实测密度）。

**存在的理由**：「一堵墙必须到过最优价吗」不该由我们定义，要用数据回归出来
（2026-09-03 用户裁定）。本模块把样本日里全部「档位堆起来又回到零」的段切出来，
按 离最优价的 tick 数 × 十档内第几档 给出分布，让密度和位置自己说话。

**判据从注册表取，不在这里重写**：候选掩码 = `core.registry.holds(spec.relation.invariants, ...)`。
上一版把 `closed & executed_vol == 0` 在三个统计函数里各抄了一遍——判据有三份就等于没有单源。

**适用时段也从注册表取**：条目声明 `windows = 连续竞价两段`，读取就按它裁。
上一版读全天，把开盘集合竞价的挂撤混进了分布里。

逻辑全在 `core.book`（纯核，进 CI）；这里只负责读与编排——读用 `io.raw.RawStore`，
不自己碰 parquet；价与流的口径取 `core.raw`，不另立一份。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import polars as pl

from ftbv2.core.book import attach_touch, attach_visibility, depth_deltas, level_episodes, quote_levels
from ftbv2.core.raw import Day, DefectLedger, Gap, ReadRequest, plan
from ftbv2.core.registry import DensityMeasurement, EvidenceRef, holds, spec
from ftbv2.io.raw import RawStore

KIND = "LevelBuildThenVanish"
"""本模块回归的是哪一条条目。定义从注册表取，这里只留键。"""

_NEEDED = {
    "orders": ("time_ms", "oid", "type", "side", "price", "vol"),
    "trades": ("time_ms", "code", "bs", "price", "vol", "ask_ref", "bid_ref"),
    "xinqing": ("time_ms", *[f"{s}_px_{i}" for s in ("ask", "bid") for i in range(1, 11)]),
}


@dataclass(frozen=True)
class DayProbe:
    """一天的回归结果。`unlinked_cancels` 是缺口，不是零——必须随分布一起看。"""

    day: Day
    n_symbols: int
    rows_read: dict[str, int]
    n_episodes: int
    n_closed: int
    n_candidates: int
    unlinked_cancels: int
    total_cancels: int
    seconds: float
    by_ticks: list[dict[str, object]]
    by_visible_level: list[dict[str, object]]
    quantiles: dict[str, object]

    @property
    def input_rows(self) -> int:
        """坍缩比的分母：该条目声明的 streams 在本日本样本上实际读入的行数之和。"""
        return sum(self.rows_read.values())


@dataclass(frozen=True)
class WallProbe:
    """一批样本日的回归结果。

    **多天是本函数的常态，不是可选项**：一天一个样本回答不了「这条结构在不同行情下稳不稳」，
    而密度是要拿去决定花不花 15 小时的（红队 2026-09-03 方法论严重 4）。
    """

    days: tuple[Day, ...]
    per_day: tuple[DayProbe, ...]
    symbol_days: int
    input_rows: int
    n_episodes: int
    n_candidates: int
    unlinked_cancels: int
    total_cancels: int
    seconds: float
    rows_per_symbol_day: float
    collapse_ratio: float
    invariants: tuple[str, ...]

    def measurement(self, evidence: EvidenceRef) -> DensityMeasurement:
        """把这批实测封成注册表准入用的实测记录。数字进收据，不进源码。"""
        return DensityMeasurement(
            kind=KIND,
            rows_per_symbol_day=self.rows_per_symbol_day,
            collapse_ratio=self.collapse_ratio,
            symbol_days=self.symbol_days,
            input_rows=self.input_rows,
            event_rows=self.n_candidates,
            evidence=evidence,
        )


def probe_walls(store: RawStore, ledger: DefectLedger, days: tuple[Day, ...],
                sample: int = 0, seed: int = 0) -> WallProbe:
    """在给定样本日上切出全部档位生命周期，按注册表的不变量数出候选，并给出分布。

    不做任何**判断**（多大算大、多快算快），只做条目已经声明的**结构约束**。

    `sample > 0` 时每天随机抽这么多标的。**标的全集取自当日 orders 实际出现的标的**——
    不能用 row group 的 symbol_min / symbol_max 当全集，那只是每个 row group 的边界值，
    抽出来的样本会系统性偏向排序边界（2026-09-03 第一次跑就踩了这个）。
    """
    per_day = tuple(_probe_one_day(store, ledger, d, sample, seed) for d in days)
    symbol_days = sum(p.n_symbols for p in per_day)
    input_rows = sum(p.input_rows for p in per_day)
    candidates = sum(p.n_candidates for p in per_day)
    return WallProbe(
        days=days,
        per_day=per_day,
        symbol_days=symbol_days,
        input_rows=input_rows,
        n_episodes=sum(p.n_episodes for p in per_day),
        n_candidates=candidates,
        unlinked_cancels=sum(p.unlinked_cancels for p in per_day),
        total_cancels=sum(p.total_cancels for p in per_day),
        seconds=round(sum(p.seconds for p in per_day), 1),
        rows_per_symbol_day=candidates / symbol_days,
        collapse_ratio=input_rows / candidates if candidates else float("inf"),
        invariants=tuple(c.value for c in spec(KIND).relation.invariants),
    )


def _probe_one_day(store: RawStore, ledger: DefectLedger, day: Day,
                   sample: int, seed: int) -> DayProbe:
    t0 = time.time()
    entry = spec(KIND)
    frames, rows, gaps = {}, {}, []
    symbols: frozenset[str] | None = None
    for stream, names in _NEEDED.items():
        req = ReadRequest(stream, (day,), names, symbols, entry.windows)   # 未登记字段由 plan() 抛 KeyError
        res = store.execute(plan(req, store.catalog(stream, (day,)), ledger))
        frames[stream], rows[stream] = res.frame, res.stats.rows
        gaps.extend(res.gaps)
        if stream == "orders" and sample:
            universe = sorted(frames["orders"]["symbol"].unique().to_list())
            symbols = frozenset(random.Random(seed).sample(universe, min(sample, len(universe))))
            frames["orders"] = frames["orders"].filter(pl.col("symbol").is_in(sorted(symbols)))
            rows["orders"] = frames["orders"].height
    _refuse_gaps(day, tuple(gaps))

    delta = depth_deltas(frames["orders"], frames["trades"])
    episodes = attach_touch(level_episodes(delta.deltas),
                            frames["xinqing"].rename({"ask_px_1": "ask1", "bid_px_1": "bid1"}))
    episodes = attach_visibility(episodes, quote_levels(frames["xinqing"]))
    episodes = episodes.with_columns(
        holds(entry.relation.invariants, tuple(episodes.columns)).alias("candidate"))
    return DayProbe(
        day=day,
        n_symbols=frames["orders"]["symbol"].n_unique(),
        rows_read=rows,
        n_episodes=episodes.height,
        n_closed=int(episodes.filter(pl.col("closed")).height),
        n_candidates=int(episodes["candidate"].sum()),
        unlinked_cancels=delta.unlinked_cancels,
        total_cancels=delta.total_cancels,
        seconds=round(time.time() - t0, 1),
        by_ticks=_by_ticks(episodes).to_dicts(),
        by_visible_level=_by_level(episodes).to_dicts(),
        quantiles=_quantiles(episodes),
    )


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


def _by_ticks(episodes: pl.DataFrame) -> pl.DataFrame:
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
        episodes.with_columns(bucket.alias("bucket"))
        .group_by("bucket")
        .agg(pl.len().alias("段数"), pl.col("candidate").sum().alias("假墙候选数"),
             pl.col("peak_vol").median().alias("peak_vol_中位"),
             pl.col("life_ms").median().alias("life_ms_中位"),
             pl.col("n_adds").median().alias("n_adds_中位"))
        .sort("段数", descending=True)
    )


def _by_level(episodes: pl.DataFrame) -> pl.DataFrame:
    """按峰值时刻该档位在十档里的第几档分组；−1 = 峰值那一帧它不在十档内（看不见）。"""
    return (
        episodes.group_by(pl.col("level").fill_null(-1).alias("可见档位"))
        .agg(pl.len().alias("段数"), pl.col("candidate").sum().alias("假墙候选数"),
             pl.col("peak_vol").median().alias("peak_vol_中位"),
             pl.col("n_adds").median().alias("n_adds_中位"))
        .sort("可见档位")
    )


def _quantiles(episodes: pl.DataFrame) -> dict[str, object]:
    hit = episodes.filter(pl.col("candidate"))
    if hit.height == 0:
        return {}
    return {
        "假墙候选_peak_vol": [int(hit["peak_vol"].quantile(q) or 0) for q in (0.5, 0.9, 0.99)],
        "假墙候选_life_ms": [int(hit["life_ms"].quantile(q) or 0) for q in (0.5, 0.9, 0.99)],
        "假墙候选_n_adds": [int(hit["n_adds"].quantile(q) or 0) for q in (0.5, 0.9, 0.99)],
        "分位": [0.5, 0.9, 0.99],
    }
