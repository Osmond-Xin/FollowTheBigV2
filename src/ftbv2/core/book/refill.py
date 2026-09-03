"""档位深度的**循环**：同一价位反复「被一笔委托堆起来 → 被成交整个吃光 → 回到零」。

这是 `RefillAfterFill`（冰山）的候选生成器。它与假墙用**同一台机器**（`level_episodes`），
只是互补地用：

| | 档位生命周期 | |
|---|---|---|
| 假墙 | 堆起来 → 整个消失，**全程零成交** | 撤走的 |
| 冰山 | 堆起来 → 整个消失，**全部由成交消失**，且反复同一个幅度 | 吃掉的 |

**为什么必须以「档位归零」为准，而不是以「某一笔委托被吃完」为准。**
2026-09-03 第一版是后者，实测 55–219 条/(标的·日)，用户当场指出「这么多条说明计算方法有问题，
肯定把常见情况也算成冰山了」——是对的，而且缺陷是具体的：

- 冰山的机制是**同一个人**把大单切片、吃完一片补一片。逐笔数据里**没有账户身份**；
- 上一版于是退而求其次，拿「同价 + 同量」当身份，结果数的是
  「同一价位上碰巧同量的委托，其中一笔被吃过」——张三挂 300 被吃掉、李四十秒后也挂 300，
  就算一轮。**两个毫不相干的人。**
- 它也从不要求该档位真的空过：底下压着别人的 500 股照样算。

**档位归零替代了身份**：中间没有别人，所以补上来的只能是同一个人。
这是一个**结构**约束（存在性），不是幅度阈值。

代价是**只认得「独占档位」的切片**：热门价位上多人共存时，冰山与普通排队在数据上
本来就分不开——分不开就不认，与「幸存即证据」「看不见的墙吓不到人」同一条纪律。
这是**假阴性**方向的取舍，如实记在这里。
"""

from __future__ import annotations

import polars as pl

_PART = ["symbol", "side", "price"]


def eaten_cycles(episodes: pl.DataFrame) -> pl.DataFrame:
    """给每个档位生命周期标注它是不是**一笔委托被成交整个吃光**。

    四条同时成立才算：回到过零 · 只由**一笔**委托堆起来 · 期间**没有撤单** ·
    离场的量全部是成交。

    「只由一笔委托堆起来」是「一片」的定义：一片就是一笔委托。三笔凑出来的深度不是一片。
    「没有撤单」把「撤掉再挂」排除出去——那是改主意，不是补片。
    """
    return episodes.with_columns(
        (pl.col("closed")
         & (pl.col("n_adds") == 1)
         & (pl.col("n_cancels") == 0)
         & (pl.col("executed_vol") >= pl.col("peak_vol"))).alias("eaten"))


def refill_chains(episodes: pl.DataFrame) -> pl.DataFrame:
    """同一 (标的, side, price) 上，**峰值量相同的相邻生命周期**连成一条链。

    产出列：symbol · side · price · chain · slice_vol · n_cycles · clean_cycles ·
    n_refills · total_filled · t_start · t_end · span_ms · fill_time_ms · refill_time_ms。

    - `clean_cycles` —— 链里有几个循环是「一笔委托被整个吃光」（`eaten_cycles` 判的）。
      不变量要求它等于 `n_cycles`：**一个不干净的循环就让整条链不算**。
    - `fill_time_ms` —— 第一个循环归零的时刻；`refill_time_ms` —— 第二个循环开始的时刻。
      不变量 `refill_strictly_after_fill` 判这两个数；单循环的链 `refill_time_ms` 是 null，
      **null 判否**，它们留在表里当分母。

    链按「峰值量相同」切，**不按干净与否切**——否则「每个循环都干净」这条不变量就成了重言式，
    表里也就没有分母了。代价是链中间夹一个不干净的循环会把它整条否掉（保守方向，不会多算）。
    """
    f = eaten_cycles(episodes).sort([*_PART, "ep"])
    prev_peak = pl.col("peak_vol").shift(1).over(_PART)
    f = f.with_columns(
        (prev_peak.is_null() | (prev_peak != pl.col("peak_vol"))).cum_sum().over(_PART).alias("chain"))
    keys = [*_PART, "chain"]
    f = f.with_columns(pl.col("ep").rank("ordinal").over(keys).alias("_cyc") - 1)
    return (
        f.group_by(keys)
        .agg(pl.col("peak_vol").first().alias("slice_vol"),
             pl.len().alias("n_cycles"),
             pl.col("eaten").sum().alias("clean_cycles"),
             pl.col("executed_vol").sum().alias("total_filled"),
             pl.col("t_start").min().alias("t_start"),
             pl.col("t_end").max().alias("t_end"),
             pl.col("t_end").filter(pl.col("_cyc") == 0).first().alias("fill_time_ms"),
             pl.col("t_start").filter(pl.col("_cyc") == 1).first().alias("refill_time_ms"))
        .with_columns((pl.col("n_cycles") - 1).alias("n_refills"),
                      (pl.col("t_end") - pl.col("t_start")).alias("span_ms"))
        .sort(keys)
    )
