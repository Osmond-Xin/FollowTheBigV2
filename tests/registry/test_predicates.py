"""不变量谓词的最小反例集。

**这是把「不变量」从门面变成判据的那一步。** 上一版 `Relation.invariant` 是自由字符串，
门禁只查非空——三路红队一致判为门面（方法论致命 2 / 工程致命 4）。现在每条码挂一段真的
能跑的 polars 表达式，本文件逐条给它一个应当被判否的最小反例：**判不否，测试就红**。

只写会红的测试：不写 `assert isinstance(code, InvariantCode)` 这种重言式。
"""

import polars as pl
import pytest

from ftbv2.core.registry import InvariantCode, holds, predicate, required_fields


def _mask(code: InvariantCode, **cols: object) -> bool:
    """把一行数据喂给某条谓词，取它的判定。"""
    frame = pl.DataFrame({k: [v] for k, v in cols.items()})
    return bool(frame.select(predicate(code).alias("m"))["m"][0])


# ------------------------------------------------- 每条码：一个应被判真、一个应被判否

def test_未回到零的档位不算消失() -> None:
    """收盘时仍挂着的档位不是「消失」，它只是还没结束。"""
    assert _mask(InvariantCode.RETURNS_TO_ZERO, closed=True)
    assert not _mask(InvariantCode.RETURNS_TO_ZERO, closed=False)


def test_存续期间有成交就不是被撤走的() -> None:
    """有成交的档位消失是被吃掉的，不是被撤走的——那是另一回事。"""
    assert _mask(InvariantCode.NO_TRADES_DURING_LIFE, executed_vol=0)
    assert not _mask(InvariantCode.NO_TRADES_DURING_LIFE, executed_vol=1)


def test_十档外的档位不算墙() -> None:
    """看不见的墙吓不到人。2026-09-03 实测：十档外占候选 87%，形态与十档内完全不同。"""
    assert _mask(InvariantCode.VISIBLE_IN_QUOTED_DEPTH_AT_PEAK, level=7)
    assert not _mask(InvariantCode.VISIBLE_IN_QUOTED_DEPTH_AT_PEAK, level=None)


def test_一笔建起来又一笔撤掉不降维() -> None:
    """单笔挂撤是单行属性判断的另一种写法。"""
    assert _mask(InvariantCode.BUILT_BY_MULTIPLE_ORDERS, n_adds=2)
    assert not _mask(InvariantCode.BUILT_BY_MULTIPLE_ORDERS, n_adds=1)


def test_补单在成交之前不是冰山() -> None:
    """**错序必须被判否**——这正是上一版「同价同量相邻两笔」放过的那一类。"""
    assert _mask(InvariantCode.REFILL_STRICTLY_AFTER_FILL, refill_time_ms=101, fill_time_ms=100)
    assert not _mask(InvariantCode.REFILL_STRICTLY_AFTER_FILL, refill_time_ms=99, fill_time_ms=100)
    assert not _mask(InvariantCode.REFILL_STRICTLY_AFTER_FILL, refill_time_ms=100, fill_time_ms=100), \
        "同毫秒不算「之后」：严格晚于才是因果"


def test_成交量不到展示量说明不了隐藏() -> None:
    assert _mask(InvariantCode.FILL_REACHES_DISPLAYED, executed_vol=100, displayed_vol=100)
    assert not _mask(InvariantCode.FILL_REACHES_DISPLAYED, executed_vol=99, displayed_vol=100)


def test_档位没幸存就没有幸存这件事() -> None:
    assert _mask(InvariantCode.LEVEL_SURVIVES_NEXT_FRAME, surviving_vol=1)
    assert not _mask(InvariantCode.LEVEL_SURVIVES_NEXT_FRAME, surviving_vol=0)


def test_两帧之间有新挂单时幸存者是新来的人() -> None:
    """3 秒穿档再被新挂单撑住，后一帧的「活着」不是暗单（红队 2026-09-03 架构严重 4）。"""
    assert _mask(InvariantCode.NO_NEW_ORDERS_BETWEEN_FRAMES, added_vol=0)
    assert not _mask(InvariantCode.NO_NEW_ORDERS_BETWEEN_FRAMES, added_vol=1)


def test_没跨过刻度的不是一根bar() -> None:
    assert _mask(InvariantCode.VOLUME_CROSSES_TICK, volume=100, tick_volume=100)
    assert not _mask(InvariantCode.VOLUME_CROSSES_TICK, volume=99, tick_volume=100)


# ------------------------------------------------- 表本身

def test_每条码都挂得出谓词与所需字段() -> None:
    """没有谓词的码就是一句话——`predicates.py` 在导入时就会拒绝这种码，这里守住反向：
    枚举里加了成员而忘了加表项，import 阶段红。"""
    for code in InvariantCode:
        assert required_fields(code), code
        assert isinstance(predicate(code), pl.Expr), code


def test_列不齐时判据不许求值() -> None:
    """**缺列硬失败，不静默判真**：判据读不到它要的列时判出来的真是假的。"""
    with pytest.raises(KeyError, match="缺少不变量所需的列"):
        holds((InvariantCode.NO_TRADES_DURING_LIFE,), ("closed",))


def test_空的一组不变量不是没有约束() -> None:
    with pytest.raises(ValueError, match="忘了写约束"):
        holds((), ("closed",))


def test_多条不变量是与不是或() -> None:
    """假墙要同时满足四条。任意一条不成立就不是候选。"""
    codes = (InvariantCode.RETURNS_TO_ZERO, InvariantCode.NO_TRADES_DURING_LIFE)
    frame = pl.DataFrame({"closed": [True, True, False], "executed_vol": [0, 5, 0]})
    got = frame.select(holds(codes, tuple(frame.columns)).alias("m"))["m"].to_list()
    assert got == [True, False, False]


def test_值缺失判否而不是判不出来() -> None:
    """`null` 的意思是「说不出来」，「说不出来」不是「成立」。
    下游 `filter` 会当假、`sum()` 会跳过，看起来对——但只要有人写 `~mask` 就悄悄反过来。"""
    frame = pl.DataFrame({"refill_time_ms": [None, 10], "fill_time_ms": [None, 5]},
                         schema={"refill_time_ms": pl.Int64, "fill_time_ms": pl.Int64})
    got = frame.select(
        holds((InvariantCode.REFILL_STRICTLY_AFTER_FILL,), tuple(frame.columns)).alias("m"))
    assert got["m"].to_list() == [False, True]
    assert got["m"].null_count() == 0
