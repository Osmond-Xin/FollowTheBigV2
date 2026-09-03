"""不变量谓词表：每条 `InvariantCode` 对应一段可执行的 polars 表达式与它所需的字段。

**存在的理由**：上一版 `Relation.invariant` 是自由字符串，机器只查非空——写 `"TODO"` 也过。
三路红队（2026-09-03 方法论致命 2 / 工程致命 4 / 架构）一致指出这是门面：
判据权交给了作者命名，契约名会自我合理化一切错误写法。

**这里怎么不是另一个门面**：每条码有 ① 一段真的能跑的表达式，② 一份它读哪些列的声明，
③ 一组最小反例（`tests/registry/test_predicates.py`）——单行过滤、错序、缺字段各自必须被判否。
调用方 `check_invariants()` 先查列是否齐全再求值，缺列硬失败，不静默判真。

**为什么谓词写在注册表而不是提取器里**：`io.events.probe` 上一版把
`closed & executed_vol == 0` 这个判据在三处各抄了一遍（`_by_ticks` / `_by_level` / `_quantiles`）。
判据有三份就等于没有单源。收进这里之后，提取器与统计都从同一个表达式取，改一处即改全部。

字段名取 `core.book` 产出的档位生命周期表的列名。这条耦合是**声明出来的**（`required_fields`），
不是藏起来的：`tests/book/test_depth.py` 校验 core.book 真的产出这些列，改名字就红。
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

import polars as pl

from ftbv2.core.raw import LOT_SIZE


class InvariantCode(Enum):
    """角色之间必须成立的关系。**一条码 = 一段可跑的表达式**，不是一句话。"""

    RETURNS_TO_ZERO = "returns_to_zero"
    """档位深度确实回到过零。收盘时仍挂着的档位不是「消失」，它只是还没结束。"""

    NO_TRADES_DURING_LIFE = "no_trades_during_life"
    """该档位存续期间一笔没成交。有成交的档位消失是被吃掉的，不是被撤走的——那是另一回事。"""

    VISIBLE_IN_QUOTED_DEPTH_AT_PEAK = "visible_in_quoted_depth_at_peak"
    """峰值时刻该档位在交易所发布的十档之内。**看不见的墙吓不到人**——
    这不是幅度阈值，十档是交易所定的发布范围，不是我们选的一个数。"""

    BUILT_BY_MULTIPLE_ORDERS = "built_by_multiple_orders"
    """建墙至少两笔委托。一笔建起来又一笔撤掉是单行属性判断的另一种写法，不降维。"""

    REFILL_STRICTLY_AFTER_FILL = "refill_strictly_after_fill"
    """补单的时刻严格晚于把前一片吃完的那笔成交。这是冰山与「密集报单里同价同量相邻」的分界。"""

    SLICE_EXCEEDS_MIN_LOT = "slice_exceeds_min_lot"
    """每片的委托量超过一手（`core.raw.LOT_SIZE`）。**一手不能再切，所以一手的重复挂单
    不是切片的证据**——2026-09-03 实测：不加这一条，冰山 218.92 条/(标的·日)，
    而每片量中位**在每一个可见档位桶、每一个轮数桶里都是 100 股**。

    ⚠️ **这一条与别的不变量性质不同，如实写在这里**：它是对「切片」这个单位本身的前置条件，
    是**组的属性**，不是角色之间的关系。降维的担子由 `REFILL_STRICTLY_AFTER_FILL` 挑；
    它只负责把「再切不下去的东西」排除出「切片」的语义。
    它之所以不是幅度阈值，是因为一手是**交易所定的最小委托单位**，与十档同类——
    不是我们从分布里挑出来的一个数。"""

    FILL_REACHES_DISPLAYED = "fill_reaches_displayed"
    """两帧之间该档成交量 ≥ 前一帧的展示量。"""

    LEVEL_SURVIVES_NEXT_FRAME = "level_survives_next_frame"
    """后一帧该档位仍有展示量。幸存本身就是隐藏的证据。"""

    NO_NEW_ORDERS_BETWEEN_FRAMES = "no_new_orders_between_frames"
    """两帧之间该档位没有新增委托。没有这一条，3 秒穿档后被新挂单重新撑住的档位
    会被当成暗单幸存（红队 2026-09-03 架构严重 4 / 方法论建议 11）。"""

    VOLUME_CROSSES_TICK = "volume_crosses_tick"
    """本 bar 累计量首次跨过刻度。量到了就结，不是时间到了就结。"""


_TABLE: dict[InvariantCode, tuple[tuple[str, ...], Callable[[], pl.Expr]]] = {
    InvariantCode.RETURNS_TO_ZERO: (("closed",), lambda: pl.col("closed")),
    InvariantCode.NO_TRADES_DURING_LIFE: (("executed_vol",), lambda: pl.col("executed_vol") == 0),
    InvariantCode.VISIBLE_IN_QUOTED_DEPTH_AT_PEAK: (("level",), lambda: pl.col("level").is_not_null()),
    InvariantCode.BUILT_BY_MULTIPLE_ORDERS: (("n_adds",), lambda: pl.col("n_adds") >= 2),
    InvariantCode.REFILL_STRICTLY_AFTER_FILL: (
        ("refill_time_ms", "fill_time_ms"), lambda: pl.col("refill_time_ms") > pl.col("fill_time_ms")),
    InvariantCode.SLICE_EXCEEDS_MIN_LOT: (
        ("slice_vol",), lambda: pl.col("slice_vol") > LOT_SIZE),
    InvariantCode.FILL_REACHES_DISPLAYED: (
        ("executed_vol", "displayed_vol"), lambda: pl.col("executed_vol") >= pl.col("displayed_vol")),
    InvariantCode.LEVEL_SURVIVES_NEXT_FRAME: (("surviving_vol",), lambda: pl.col("surviving_vol") > 0),
    InvariantCode.NO_NEW_ORDERS_BETWEEN_FRAMES: (("added_vol",), lambda: pl.col("added_vol") == 0),
    InvariantCode.VOLUME_CROSSES_TICK: (
        ("volume", "tick_volume"), lambda: pl.col("volume") >= pl.col("tick_volume")),
}

if set(_TABLE) != set(InvariantCode):
    raise ValueError(f"不变量码没有谓词：{sorted(c.value for c in set(InvariantCode) - set(_TABLE))}")


def required_fields(code: InvariantCode) -> tuple[str, ...]:
    """该谓词要读哪些列。调用方据此校验候选表，缺列硬失败——不静默判真。"""
    return _TABLE[code][0]


def predicate(code: InvariantCode) -> pl.Expr:
    """该不变量的可执行表达式。"""
    return _TABLE[code][1]()


def holds(codes: tuple[InvariantCode, ...], columns: tuple[str, ...]) -> pl.Expr:
    """把一组不变量与成一个掩码。

    **列不齐就抛**：判据读不到它要的列时，判出来的真是假的。

    **值缺失判否**（`fill_null(False)`）。这不是防御性编程，是语义：
    `refill_time_ms > fill_time_ms` 在「这一组一笔都没吃完」时两边都是 null，
    polars 给出的是 null 而不是 False——而 null 的意思是「说不出来」，
    「说不出来」就**不是**「成立」。不填的话下游 `filter` 会当假、`sum()` 会跳过，
    看起来对，但只要有人写 `~mask` 就悄悄反过来了。
    """
    missing = sorted({f for c in codes for f in required_fields(c)} - set(columns))
    if missing:
        raise KeyError(f"候选表缺少不变量所需的列：{missing}；已有 {sorted(columns)}")
    if not codes:
        raise ValueError("一组空的不变量恒真：这不是「没有约束」，是「忘了写约束」")
    expr = predicate(codes[0])
    for c in codes[1:]:
        expr = expr & predicate(c)
    return expr.fill_null(value=False)
