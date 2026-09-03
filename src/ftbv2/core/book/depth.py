"""从逐笔流重建每个 (标的, side, price) 档位的深度时间线，切出「从 0 堆起来 → 回到 0」的区间。

这是 `LevelBuildThenVanish`（假墙）的候选生成器，也是密度回归的对象：**墙不是一笔委托，是一个档位**。
纯逻辑核——只吃 DataFrame，不碰 IO；列名与 dtype 全部取自 `core.raw`，不另立一份口径。

**两个交易所把深度变化记在不同的流里**（2026-09-03 实测 20220104，非从字段名推断）：

| | 新增 | 撤单 | 成交 |
|---|---|---|---|
| SH（`600/601/603/605`） | `orders.type == "A"`，自带价与量 | `orders.type == "D"`，**自带价与量** | `trades`（`code` 全为 `\\x00`） |
| SZ（`000/001/002/003`） | `orders` 全部行 | `trades.code == "C"`，**price 恒为 0**，只带被撤委托号 | `trades.code == "0"` |

⇒ SH 的档位重建不需要任何关联；**SZ 的撤单必须用 `oid` 关联回 orders 才知道撤在哪个档位**，
实测命中率约 85%，未命中的撤单**计数上报、不静默丢弃**（`unlinked_cancels`）。
这与红队 2026-09-03 的判断相反：它说上交所缺关联字段所以更难，实测是两边各有各的难处，且难在不同结构上。

成交消耗的是**被动方**的深度：`trades.bs` 是主动方向，所以减的是它的反向。
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from ftbv2.core.raw import PRICE_SCALE

TICK = PRICE_SCALE // 100
"""最小价位变动 = 0.01 元，在定点整数里是 100。与 core.raw.PRICE_SCALE 同源，不另写字面量。"""

_SH_SUFFIX = ".SH"
_ADD, _CANCEL, _TRADE = "add", "cancel", "trade"


@dataclass(frozen=True)
class DeltaResult:
    """深度增量流 + 无法归档位的撤单计数。后者是缺口，不是零——「查不到 = 没有」被禁止。"""

    deltas: pl.DataFrame
    unlinked_cancels: int
    total_cancels: int


def depth_deltas(orders: pl.DataFrame, trades: pl.DataFrame) -> DeltaResult:
    """把 orders 与 trades 归一成一条深度增量流：(symbol, side, price, time_ms, ord, oid, delta, reason)。

    `ord` 是源行序，作为同毫秒的稳定次序——没有它，同一毫秒的多笔在不同切分下会改变
    「谁先把档位堆到零」，结果不可复现。
    `side` 统一为委托所在的方向（成交行已折算成被动方）。

    **`oid` 是这一行说的是哪一笔委托**：新增与上交所撤单是它自己的委托号；深交所撤单是
    被撤的那一笔（由 `_link_sz_cancels` 关联出来）；成交行是**被动方**的委托号
    （`bs` 是主动方向，所以主买取 `ask_ref`、主卖取 `bid_ref`）。
    冰山要判「某一笔本方委托被成交耗尽」，靠的就是这一列——**它在这里算一次，
    不在下游各算一遍**：哪一行属于哪一笔委托，是两个交易所口径差异的一部分，
    与 side / price 的归一同源。
    """
    o = orders.with_row_index("ord")
    t = trades.with_row_index("ord", offset=o.height)
    sh = pl.col("symbol").str.ends_with(_SH_SUFFIX)

    adds = o.filter(~sh | (pl.col("type") == "A")).select(
        "symbol", "side", "price", "time_ms", "ord", "oid",
        pl.col("vol").alias("delta"), pl.lit(_ADD).alias("reason"))
    sh_cancels = o.filter(sh & (pl.col("type") == "D")).select(
        "symbol", "side", "price", "time_ms", "ord", "oid",
        (-pl.col("vol")).alias("delta"), pl.lit(_CANCEL).alias("reason"))

    executed = t.filter(pl.col("code") != "C").select(
        "symbol", pl.when(pl.col("bs") == "B").then(pl.lit("S")).otherwise(pl.lit("B")).alias("side"),
        "price", "time_ms", "ord",
        pl.when(pl.col("bs") == "B").then(pl.col("ask_ref")).otherwise(pl.col("bid_ref")).alias("oid"),
        (-pl.col("vol")).alias("delta"), pl.lit(_TRADE).alias("reason"))

    sz_raw = t.filter(pl.col("code") == "C")
    sz_cancels, unlinked = _link_sz_cancels(sz_raw, o)
    total_cancels = sh_cancels.height + sz_raw.height

    deltas = pl.concat([adds, sh_cancels, executed, sz_cancels], how="vertical").sort("symbol", "side", "price", "ord")
    return DeltaResult(deltas, unlinked, total_cancels)


def _link_sz_cancels(sz_raw: pl.DataFrame, orders: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """深交所撤单行不带价格，只带被撤委托号（ask_ref / bid_ref 里非零的那个）——必须关联回 orders 取档位。"""
    ref = sz_raw.with_columns(
        pl.when(pl.col("bid_ref") != 0).then(pl.col("bid_ref")).otherwise(pl.col("ask_ref")).alias("oid"))
    book = orders.select("symbol", "oid", pl.col("price").alias("o_price"), pl.col("side").alias("o_side"))
    joined = ref.join(book, on=["symbol", "oid"], how="left")
    linked = joined.filter(pl.col("o_price").is_not_null())
    return (
        linked.select("symbol", pl.col("o_side").alias("side"), pl.col("o_price").alias("price"), "time_ms", "ord",
                      "oid", (-pl.col("vol")).alias("delta"), pl.lit(_CANCEL).alias("reason")),
        joined.height - linked.height,
    )


def level_episodes(deltas: pl.DataFrame) -> pl.DataFrame:
    """档位生命周期：同一 (symbol, side, price) 上，深度从 0 堆起来到回落至 0 的一段。

    每段带：起止与峰值时刻 · peak_vol · n_adds · n_cancels · executed_vol · life_ms · closed。

    `closed` 是**这一段有没有真的回到零**：收盘时仍挂着的档位不是「消失」，它只是还没结束。
    假墙候选 = `closed ∧ executed_vol == 0`（建起来、整个消失、中间一笔没成交）。
    这里不做任何筛选：筛选是判断，属于因子层；本函数只切段并如实标注，供密度回归看分布。

    `depth < 0` 的段说明该档位在窗口开始前就有挂单（成交减掉了看不见的存量），
    `peak_vol > 0` 把它们排除在候选外，但它们仍出现在结果里、由调用方看得见——不静默丢。
    """
    part = ["symbol", "side", "price"]
    cum = deltas.with_columns(pl.col("delta").cum_sum().over(part).alias("depth"))
    cum = cum.with_columns(pl.col("depth").shift(1, fill_value=0).over(part).alias("prev"))
    cum = cum.with_columns((pl.col("prev") <= 0).cum_sum().over(part).alias("ep"))
    return (
        cum.group_by([*part, "ep"])
        .agg(
            pl.col("time_ms").min().alias("t_start"),
            pl.col("time_ms").max().alias("t_end"),
            pl.col("time_ms").get(pl.col("depth").arg_max()).alias("t_peak"),
            pl.col("depth").max().alias("peak_vol"),
            (pl.col("reason") == _ADD).sum().alias("n_adds"),
            (pl.col("reason") == _CANCEL).sum().alias("n_cancels"),
            pl.when(pl.col("reason") == _TRADE).then(-pl.col("delta")).otherwise(0).sum().alias("executed_vol"),
            pl.len().alias("n_deltas"),
            pl.col("depth").last().alias("_last_depth"),
        )
        .with_columns((pl.col("t_end") - pl.col("t_start")).alias("life_ms"),
                      (pl.col("_last_depth") <= 0).alias("closed"))
        .drop("_last_depth")
        .filter(pl.col("peak_vol") > 0)
        .sort(part + ["ep"])
    )


def attach_touch(episodes: pl.DataFrame, quotes: pl.DataFrame, at: str = "t_peak") -> pl.DataFrame:
    """给每段接上「参考时刻离本方最优价几个 tick」。`at` 是拿哪个时刻去对齐快照
    （假墙用峰值时刻 `t_peak`，别的结构用它自己的参考时刻）。

    最优价取自**峰值时刻之前最近一帧**十档快照（asof join，backward）——快照 3 秒一帧，
    所以这是一个近似：真实的最优价可能在两帧之间变过。度量名如实反映测量对象
    （`ticks_from_touch_at_nearest_frame`），不假装是瞬时值；对齐误差本身记进
    `frame_age_ms`，让下游能看见它有多近似。红队 2026-09-03 方法论第 12 条。

    quotes 需含：symbol · time_ms · ask1 · bid1（`core.raw.FIELDS["xinqing"]` 的语义名）。
    """
    q = quotes.select("symbol", pl.col("time_ms").alias("q_time"), "ask1", "bid1").sort("q_time")
    ep = episodes.sort(at)
    out = ep.join_asof(q, left_on=at, right_on="q_time", by="symbol", strategy="backward")
    touch = pl.when(pl.col("side") == "B").then(pl.col("bid1")).otherwise(pl.col("ask1"))
    return out.with_columns(
        ((pl.col("price") - touch).abs() // TICK).alias("ticks_from_touch_at_nearest_frame"),
        (pl.col(at) - pl.col("q_time")).alias("frame_age_ms"),
    ).drop("ask1", "bid1")


def attach_visibility(episodes: pl.DataFrame, levels: pl.DataFrame) -> pl.DataFrame:
    """给每段接上「峰值时刻该档位在十档里的第几档」，不在十档内则为 null。

    十档是**交易所定的可见范围**，不是我们选的一个数——看不见的墙吓不到人。
    所以「可见性」是结构约束（存在性），不是幅度阈值：它满足三条判据里的维度切分，
    也不含任何关于该量在样本中排名的判断。

    `episodes` 需含 `attach_touch` 产出的 `q_time`（峰值前最近一帧的时刻）。
    `levels` 是十档长表：symbol · q_time · side · level · px。
    """
    hit = levels.select("symbol", "q_time", "side", pl.col("px").alias("price"), "level").unique(
        subset=["symbol", "q_time", "side", "price"], keep="first")
    return episodes.join(hit, on=["symbol", "q_time", "side", "price"], how="left")


def quote_levels(quotes: pl.DataFrame, depth: int = 10) -> pl.DataFrame:
    """十档宽表 → 长表 symbol · q_time · side · level · px。列名取 core.raw.FIELDS["xinqing"] 的语义名。"""
    frames = [
        quotes.select("symbol", pl.col("time_ms").alias("q_time"), pl.lit(sd).alias("side"),
                      pl.lit(i).alias("level"), pl.col(f"{px}_px_{i}").alias("px"))
        for sd, px in (("B", "bid"), ("S", "ask")) for i in range(1, depth + 1)
    ]
    return pl.concat(frames, how="vertical").filter(pl.col("px") > 0)
