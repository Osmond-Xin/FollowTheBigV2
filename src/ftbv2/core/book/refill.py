"""委托级的成交进度，以及「同价同量、成交耗尽之后才补」的连跑。

这是 `RefillAfterFill`（冰山）的候选生成器。**冰山不是「连着报两笔一样的」，是「成交一片、补一片」**——
上一版只要求同价同量相邻两笔，那在密集报单里满地都是，与是否成交无关，切出来的不是冰山。

**关联这件事上，上一版的结论是错的。** 要判「某一笔本方委托被成交耗尽」，就得知道每一笔成交
吃的是谁：由 `depth_deltas` 的 `oid` 列回答（主动方向的反向取 `ask_ref` / `bid_ref`），
在那里算一次，这里不重算。本模块仍**按交易所分别上报关联率**（`LinkStats`），
因为「能不能合成一个数」本身要由数据说，而不是由印象说。

2026-01-04 实测（50 只随机主板标的，SH 68.7 万笔成交 / SZ 55.7 万笔）：

| | `ask_ref` | `bid_ref` | 双边都关联上 | **被动方那一边** |
|---|---:|---:|---:|---:|
| SH | 60.18% | 51.81% | 11.99% | **100.00%** |
| SZ | 100% | 100% | 100% | **100.00%** |

⇒ 上交所**只有被动方那个号是委托号**，主动方那个不是；深交所两边都是。
上一版记的「上交所单边 57% / 双边 14%」量的是**混了主被动的聚合**，
由此得出「上交所不可靠、冰山实测必须分交易所报否则会把不可靠藏起来」——
**方向对，结论错**：冰山要的恰好就是被动方（「本方委托」就是挂在那里被吃的那一笔），
而它在两个市场上都是 100%。分交易所报仍然要做，但理由变成了「让这件事可核」，
不是「因为上交所不可靠」。

**这里不做筛选**：`same_size_runs()` 把同价同量的连跑全部切出来，
「有没有真的完成过一轮成交 → 补单」由注册表的不变量判。没完成的那些也留在结果里，
让分母看得见——只报分子的比例是不能核的。
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

_ADD, _TRADE = "add", "trade"
_SH_SUFFIX = ".SH"


@dataclass(frozen=True)
class LinkStats:
    """成交 → 委托的关联率，**按交易所分开**。

    `traded_rows` 是成交行数，`linked_rows` 是其中 `oid` 能在当日 orders 里找到对应委托的行数。
    两个市场差得很远（红队 2026-09-03 指出方向，实测确认），
    所以「某一笔本方委托被成交耗尽」这个判断在两边的可信度不同——**这不是一个可以合成的数**。
    """

    by_exchange: dict[str, dict[str, int]]

    def rate(self, exchange: str) -> float:
        """该交易所的关联率。没有成交行时返回 0.0，分母写在 `traded_rows` 里，自己看。"""
        got = self.by_exchange.get(exchange, {"traded_rows": 0, "linked_rows": 0})
        return got["linked_rows"] / got["traded_rows"] if got["traded_rows"] else 0.0


def order_fills(deltas: pl.DataFrame) -> tuple[pl.DataFrame, LinkStats]:
    """每一笔委托被吃掉了多少、最后一口是什么时候。

    产出列：symbol · side · price · oid · ord · t_order · order_vol · filled_vol ·
    `t_exhaust`（累计成交首次达到委托量的那一刻，没吃完则 null）· `exhausted`。

    **「耗尽」按累计成交量 ≥ 委托量判**，不按「委托从盘口消失」判——撤单也会让它消失，
    那是另一回事（那是假墙）。
    """
    adds = deltas.filter(pl.col("reason") == _ADD).select(
        "symbol", "side", "price", "oid", "ord",
        pl.col("time_ms").alias("t_order"), pl.col("delta").alias("order_vol"))
    trades = deltas.filter(pl.col("reason") == _TRADE)
    stats = _link_stats(trades, adds)

    eaten = (
        trades.select("symbol", "oid", "ord", "time_ms", pl.col("delta").mul(-1).alias("vol"))
        .join(adds.select("symbol", "oid", "order_vol"), on=["symbol", "oid"], how="inner")
        .sort("ord")
        .with_columns(pl.col("vol").cum_sum().over("symbol", "oid").alias("cum_filled"))
    )
    totals = eaten.group_by("symbol", "oid").agg(pl.col("vol").sum().alias("filled_vol"))
    exhaust = (
        eaten.filter(pl.col("cum_filled") >= pl.col("order_vol"))
        .group_by("symbol", "oid").agg(pl.col("time_ms").first().alias("t_exhaust"))
    )
    return (
        adds.join(totals, on=["symbol", "oid"], how="left")
        .join(exhaust, on=["symbol", "oid"], how="left")
        .with_columns(pl.col("filled_vol").fill_null(0),
                      pl.col("t_exhaust").is_not_null().alias("exhausted")),
        stats,
    )


def same_size_runs(fills: pl.DataFrame) -> pl.DataFrame:
    """同一 (标的, side, price) 上，委托量相同的连跑。一条 run 就是一组候选。

    产出列：symbol · side · price · run · slice_vol · n_orders · total_filled ·
    t_start · t_end · span_ms · `fill_time_ms` · `refill_time_ms` · n_refills。

    这两个时刻就是不变量 `refill_strictly_after_fill` 要判的东西：

    - `fill_time_ms` —— run 里**第一笔被吃完**的时刻；一笔都没吃完则 null；
    - `refill_time_ms` —— 那之后**第一笔新委托**的时刻；没有则 null。

    **null 判否**，所以「一笔都没吃完」与「吃完之后再没补过」都不是候选；
    它们仍留在表里当分母——只报分子的比例是不能核的。

    ⚠️ **结组原因（`GroupCloseReason`）不在这里算。** 换量与当日边界分得出来，
    「该价位被穿过」要盘口状态，本函数只吃委托流。密度回归不需要它，
    与其发一个三缺一的枚举，不如等提取器实现时一次做对。
    """
    part = ["symbol", "side", "price"]
    f = fills.sort([*part, "ord"])
    prev_vol = pl.col("order_vol").shift(1).over(part)
    f = f.with_columns(
        (prev_vol.is_null() | (prev_vol != pl.col("order_vol"))).cum_sum().over(part).alias("run"))

    keys = [*part, "run"]
    first_fill = f.group_by(keys).agg(pl.col("t_exhaust").min().alias("fill_time_ms"))
    refill = (
        f.join(first_fill, on=keys, how="left")
        .filter(pl.col("t_order") > pl.col("fill_time_ms"))
        .group_by(keys).agg(pl.col("t_order").min().alias("refill_time_ms"),
                            pl.len().alias("n_refills"))
    )
    return (
        f.group_by(keys)
        .agg(pl.col("order_vol").first().alias("slice_vol"),
             pl.len().alias("n_orders"),
             pl.col("filled_vol").sum().alias("total_filled"),
             pl.col("t_order").min().alias("t_start"),
             pl.col("t_order").max().alias("t_end"),
             pl.col("ord").min().alias("ord"))
        .join(first_fill, on=keys, how="left")
        .join(refill, on=keys, how="left")
        .with_columns((pl.col("t_end") - pl.col("t_start")).alias("span_ms"),
                      pl.col("n_refills").fill_null(0))
        .sort(keys)
    )


def _link_stats(trades: pl.DataFrame, adds: pl.DataFrame) -> LinkStats:
    """按交易所数成交行的关联率。**分交易所报，不合成一个数。**"""
    known = adds.select("symbol", "oid").unique().with_columns(pl.lit(1).alias("_hit"))
    tagged = (
        trades.select("symbol", "oid")
        .join(known, on=["symbol", "oid"], how="left")
        .with_columns(pl.when(pl.col("symbol").str.ends_with(_SH_SUFFIX))
                      .then(pl.lit("SH")).otherwise(pl.lit("SZ")).alias("exchange"))
    )
    got = tagged.group_by("exchange").agg(
        pl.len().alias("traded_rows"), pl.col("_hit").fill_null(0).sum().alias("linked_rows"))
    return LinkStats({r["exchange"]: {"traded_rows": int(r["traded_rows"]),
                                      "linked_rows": int(r["linked_rows"])}
                      for r in got.to_dicts()})
