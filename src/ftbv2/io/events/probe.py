"""假墙密度回归（`LevelBuildThenVanish` 的候选生成 + 分布统计）。

**存在的理由**：「一堵墙必须到过最优价吗」不该由我们定义，要用数据回归出来
（2026-09-03 用户裁定）。本模块把一天里全部「档位堆起来又回到零」的段切出来，
按 离最优价的 tick 数 × 是否零成交 给出分布，让密度和位置自己说话。

逻辑全在 `core.book`（纯核，进 CI）；这里只负责读与编排——读用 `io.raw.RawStore`，
不自己碰 parquet；价与流的口径取 `core.raw`，不另立一份。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import polars as pl

from ftbv2.core.book import attach_touch, attach_visibility, depth_deltas, level_episodes, quote_levels
from ftbv2.core.raw import Day, DefectLedger, ReadRequest, plan
from ftbv2.io.raw import RawStore

_NEEDED = {
    "orders": ("time_ms", "oid", "type", "side", "price", "vol"),
    "trades": ("time_ms", "code", "bs", "price", "vol", "ask_ref", "bid_ref"),
    "xinqing": ("time_ms", *[f"{s}_px_{i}" for s in ("ask", "bid") for i in range(1, 11)]),
}


@dataclass(frozen=True)
class WallProbe:
    """一天一批标的的回归结果。`unlinked_cancels` 是缺口，不是零——必须随分布一起看。"""

    day: Day
    n_symbols: int
    rows_read: dict[str, int]
    n_episodes: int
    n_closed: int
    n_wall_candidates: int
    unlinked_cancels: int
    total_cancels: int
    seconds: float
    by_ticks: list[dict[str, object]]
    by_visible_level: list[dict[str, object]]
    quantiles: dict[str, object]


def probe_walls(store: RawStore, ledger: DefectLedger, day: Day,
                sample: int = 0, seed: int = 0) -> WallProbe:
    """切出该日全部档位生命周期，按离最优价的 tick 数分桶统计。不做任何筛选——筛选是判断。

    `sample > 0` 时随机抽这么多标的。**标的全集取自当日 orders 实际出现的标的**——
    不能用 row group 的 symbol_min / symbol_max 当全集，那只是每个 row group 的边界值，
    抽出来的样本会系统性偏向排序边界（2026-09-03 第一次跑就踩了这个）。
    """
    t0 = time.time()
    frames, rows = {}, {}
    symbols: frozenset[str] | None = None
    for stream, names in _NEEDED.items():
        req = ReadRequest(stream, (day,), names, symbols)          # 未登记字段由 plan() 抛 KeyError，不静默丢
        res = store.execute(plan(req, store.catalog(stream, (day,)), ledger))
        frames[stream], rows[stream] = res.frame, res.stats.rows
        if stream == "orders" and sample:
            universe = sorted(frames["orders"]["symbol"].unique().to_list())
            symbols = frozenset(random.Random(seed).sample(universe, min(sample, len(universe))))
            frames["orders"] = frames["orders"].filter(pl.col("symbol").is_in(sorted(symbols)))
            rows["orders"] = frames["orders"].height

    delta = depth_deltas(frames["orders"], frames["trades"])
    episodes = level_episodes(delta.deltas)
    quotes = frames["xinqing"].rename({"ask_px_1": "ask1", "bid_px_1": "bid1"})
    episodes = attach_touch(episodes, quotes)
    episodes = attach_visibility(episodes, quote_levels(frames["xinqing"].rename({"ask_px_1": "ask_px_1"})))
    return WallProbe(
        day=day,
        n_symbols=frames["orders"]["symbol"].n_unique(),
        rows_read=rows,
        n_episodes=episodes.height,
        n_closed=int(episodes.filter(pl.col("closed")).height),
        n_wall_candidates=int(episodes.filter(pl.col("closed") & (pl.col("executed_vol") == 0)).height),
        unlinked_cancels=delta.unlinked_cancels,
        total_cancels=delta.total_cancels,
        seconds=round(time.time() - t0, 1),
        by_ticks=_by_ticks(episodes).to_dicts(),
        by_visible_level=_by_level(episodes).to_dicts(),
        quantiles=_quantiles(episodes),
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
        episodes.with_columns(bucket.alias("bucket"),
                              (pl.col("closed") & (pl.col("executed_vol") == 0)).alias("wall"))
        .group_by("bucket")
        .agg(pl.len().alias("段数"), pl.col("wall").sum().alias("假墙候选数"),
             pl.col("peak_vol").median().alias("peak_vol_中位"),
             pl.col("life_ms").median().alias("life_ms_中位"),
             pl.col("n_adds").median().alias("n_adds_中位"))
        .sort("段数", descending=True)
    )


def _by_level(episodes: pl.DataFrame) -> pl.DataFrame:
    """按峰值时刻该档位在十档里的第几档分组；null = 峰值那一帧它不在十档内（看不见）。"""
    return (
        episodes.with_columns((pl.col("closed") & (pl.col("executed_vol") == 0)).alias("wall"))
        .group_by(pl.col("level").fill_null(-1).alias("可见档位"))
        .agg(pl.len().alias("段数"), pl.col("wall").sum().alias("假墙候选数"),
             pl.col("peak_vol").median().alias("peak_vol_中位"),
             pl.col("n_adds").median().alias("n_adds_中位"))
        .sort("可见档位")
    )


def _quantiles(episodes: pl.DataFrame) -> dict[str, object]:
    zero = episodes.filter(pl.col("closed") & (pl.col("executed_vol") == 0))
    if zero.height == 0:
        return {}
    return {
        "假墙候选_peak_vol": [int(zero["peak_vol"].quantile(q) or 0) for q in (0.5, 0.9, 0.99)],
        "假墙候选_life_ms": [int(zero["life_ms"].quantile(q) or 0) for q in (0.5, 0.9, 0.99)],
        "假墙候选_n_adds": [int(zero["n_adds"].quantile(q) or 0) for q in (0.5, 0.9, 0.99)],
        "分位": [0.5, 0.9, 0.99],
    }
