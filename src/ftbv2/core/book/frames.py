"""快照帧之间每个档位发生了什么：展示量 → 成交 → 新增委托 → 还剩多少。

这是 `FillExceedsDisplayed`（隐藏深度）的候选生成器。三秒一帧的十档快照只说得出
「这一帧该档挂着多少」；一个档位被吃掉全部展示量之后**还活着**这件事，要靠帧间比对才看得出来。

**为什么要读 orders**（红队 2026-09-03 架构严重 4 / 方法论建议 11）：三秒里价格可以穿过该档，
再被**新挂单**重新撑住。那时后一帧看到的「活着」是新来的人，不是暗单。
所以除了「成交 ≥ 前帧展示量」与「后帧仍在」，还必须要求**两帧之间该档没有新增委托**。
单靠 xinqing + trades 物理上判不出来这一条。

**成交与新增都取自 `depth_deltas` 的归一流**，不在这里重写一遍两个交易所的口径——
SH 撤单在 orders、SZ 撤单在 trades 且要按 oid 关联，那套已经在 `depth.py` 吸收过一次。
"""

from __future__ import annotations

import polars as pl

_ADD, _TRADE = "add", "trade"


def frame_levels(quotes: pl.DataFrame, depth: int = 10) -> pl.DataFrame:
    """十档宽表 → 长表 symbol · frame · q_time · side · price · displayed_vol。

    `frame` 是该标的当日快照的序号（按 q_time 升序，从 0 起）——帧间比对靠它错位一格，
    不靠「时间差等于 3 秒」：**帧间隔是数据说了算的，不是标称值**。
    价或量为 0 的档位不算存在（十档没填满时补 0）。
    """
    long = pl.concat([
        quotes.select(
            "symbol", pl.col("time_ms").alias("q_time"), pl.lit(sd).alias("side"),
            pl.col(f"{px}_px_{i}").alias("price"), pl.col(f"{px}_sz_{i}").alias("displayed_vol"))
        for sd, px in (("B", "bid"), ("S", "ask")) for i in range(1, depth + 1)
    ], how="vertical").filter((pl.col("price") > 0) & (pl.col("displayed_vol") > 0))
    frames = (
        quotes.select("symbol", pl.col("time_ms").alias("q_time")).unique()
        .sort("symbol", "q_time")
        .with_columns(pl.col("q_time").cum_count().over("symbol").alias("frame") - 1)
    )
    return long.join(frames, on=["symbol", "q_time"], how="inner")


def frame_transitions(levels: pl.DataFrame, deltas: pl.DataFrame) -> pl.DataFrame:
    """每个 (标的, side, price) 在相邻两帧之间的变化。

    产出列：symbol · side · price · frame · q_time · next_q_time · frame_gap_ms ·
    displayed_vol（前帧）· surviving_vol（后帧，不在盘口则 0）· executed_vol · added_vol。

    `executed_vol` / `added_vol` 取自 `depth_deltas` 的归一流，区间是 **[前帧, 后帧)**——
    与前帧同一毫秒的增量算进本段。这一刀的位置是约定不是事实：快照落在哪一毫秒、
    同毫秒的成交进没进这一帧，数据本身说不出来。写下来，让它可被质疑。

    没有下一帧的那一帧（当日最后一帧）不产出行：**比不了就不是缺证据，是没有这一次比对**。

    ⚠️ 一整帧十档全空（停牌心跳）时该帧不在 `levels` 里，它前一帧因此也不产出行；
    这类日子由 `core.registry.yields_events()` 在更上层短路，不指望本函数处理。
    """
    nxt = levels.select("symbol", "side", "price", (pl.col("frame") - 1).alias("frame"),
                        pl.col("displayed_vol").alias("surviving_vol"),
                        pl.col("q_time").alias("next_q_time"))
    ends = levels.select("symbol", "frame", pl.col("q_time").alias("next_q_time")).unique()
    ends = ends.with_columns((pl.col("frame") - 1).alias("frame"))
    pairs = (
        levels.join(ends, on=["symbol", "frame"], how="inner")
        .join(nxt, on=["symbol", "side", "price", "frame", "next_q_time"], how="left")
        .with_columns(pl.col("surviving_vol").fill_null(0))
    )
    moved = _between_frames(levels, deltas)
    return (
        pairs.join(moved, on=["symbol", "side", "price", "frame"], how="left")
        .with_columns(pl.col("executed_vol").fill_null(0), pl.col("added_vol").fill_null(0),
                      (pl.col("next_q_time") - pl.col("q_time")).alias("frame_gap_ms"))
        .sort("symbol", "frame", "side", "price")
    )


def _between_frames(levels: pl.DataFrame, deltas: pl.DataFrame) -> pl.DataFrame:
    """把每条深度增量归到它落在哪两帧之间，再按 (标的, side, price, 帧) 汇总成交与新增。

    归属用 asof（backward）：时刻 t 的增量归给**最后一个 q_time ≤ t 的帧**，即区间 [帧, 下一帧)。
    第一帧之前的增量没有前帧可比，归属为空，被 inner 语义自然排除。
    """
    frames = (levels.select("symbol", "q_time", "frame").unique()
              .sort("q_time"))
    events = deltas.filter(pl.col("reason").is_in([_ADD, _TRADE])).sort("time_ms")
    tagged = events.join_asof(frames, left_on="time_ms", right_on="q_time", by="symbol",
                              strategy="backward")
    return (
        tagged.filter(pl.col("frame").is_not_null())
        .group_by("symbol", "side", "price", "frame")
        .agg(
            pl.when(pl.col("reason") == _TRADE).then(-pl.col("delta")).otherwise(0).sum()
              .alias("executed_vol"),
            pl.when(pl.col("reason") == _ADD).then(pl.col("delta")).otherwise(0).sum()
              .alias("added_vol"),
        )
    )
