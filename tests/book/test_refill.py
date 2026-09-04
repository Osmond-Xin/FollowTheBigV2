"""core.book.refill 的契约测试：冰山的分界线是**档位反复归零**，不是「同价同量相邻」。

2026-09-03 用户当场指出上一版「这么多条说明计算方法有问题，肯定把常见情况也算成冰山了」。
缺陷是具体的：逐笔数据里没有账户身份，上一版拿「同价 + 同量」当身份，
于是「张三挂 300 被吃掉、李四十秒后也挂 300」算一轮——两个毫不相干的人。
本文件给那一类一个真会红的反例：**档位没归零就不算**。
"""

import polars as pl

from ftbv2.core.book import depth_deltas, eaten_cycles, level_episodes, refill_chains
from ftbv2.core.registry import holds, spec

SZ = "000001.SZ"
_INV = spec("RefillAfterFill").relation.invariants

_O = {"symbol": pl.Utf8, "side": pl.Utf8, "price": pl.Int64, "time_ms": pl.Int64,
      "oid": pl.Int64, "type": pl.Utf8, "vol": pl.Int64}
_T = {"symbol": pl.Utf8, "price": pl.Int64, "time_ms": pl.Int64, "code": pl.Utf8,
      "bs": pl.Utf8, "vol": pl.Int64, "ask_ref": pl.Int64, "bid_ref": pl.Int64}


def _orders(times, vols, oids=None, price=1000):
    n = len(times)
    return pl.DataFrame({"symbol": [SZ] * n, "side": ["B"] * n, "price": [price] * n,
                         "time_ms": times, "oid": oids or list(range(1, n + 1)),
                         "type": ["0"] * n, "vol": vols}, schema=_O)


def _fills(times, vols, price=1000):
    """主动卖单吃掉买方挂单。"""
    n = len(times)
    return pl.DataFrame({"symbol": [SZ] * n, "price": [price] * n, "time_ms": times,
                         "code": ["0"] * n, "bs": ["S"] * n, "vol": vols,
                         "ask_ref": [0] * n, "bid_ref": [0] * n}, schema=_T)


def _cancels(times, vols, oids, price=0):
    n = len(times)
    return pl.DataFrame({"symbol": [SZ] * n, "price": [price] * n, "time_ms": times,
                         "code": ["C"] * n, "bs": [" "] * n, "vol": vols,
                         "ask_ref": [0] * n, "bid_ref": oids}, schema=_T)


def _chains(orders, trades):
    c = refill_chains(level_episodes(depth_deltas(orders, trades).deltas))
    return c.with_columns(holds(_INV, tuple(c.columns)).alias("hit"))


def _empty_trades():
    return pl.DataFrame(schema=_T)


# ------------------------------------------------- 一个循环是什么

def test_一笔委托被整个吃光才算一个干净循环() -> None:
    ep = level_episodes(depth_deltas(_orders([1], [300]), _fills([5], [300])).deltas)
    assert eaten_cycles(ep)["eaten"].to_list() == [True]


def test_被撤走的循环不算() -> None:
    """撤掉再挂是改主意，不是补片。"""
    ep = level_episodes(depth_deltas(_orders([1], [300]), _cancels([5], [300], [1])).deltas)
    assert eaten_cycles(ep)["eaten"].to_list() == [False]


def test_只吃掉一半的循环不算() -> None:
    o = _orders([1], [300])
    t = pl.concat([_fills([5], [100]), _cancels([9], [200], [1])])
    ep = level_episodes(depth_deltas(o, t).deltas)
    assert eaten_cycles(ep)["eaten"].to_list() == [False]


def test_三笔凑出来的深度不是一片() -> None:
    """一片就是一笔委托。三笔凑出 300 再被吃光，那不是切片。"""
    o = _orders([1, 2, 3], [100, 100, 100])
    ep = level_episodes(depth_deltas(o, _fills([9], [300])).deltas)
    assert eaten_cycles(ep)["eaten"].to_list() == [False]


# ------------------------------------------------- 冰山的分界线

def test_档位反复归零同幅度才是冰山() -> None:
    o = _orders([1, 10], [300, 300])
    t = _fills([5, 15], [300, 300])
    row = _chains(o, t).row(0, named=True)
    assert row["slice_vol"] == 300 and row["n_cycles"] == 2 and row["n_refills"] == 1
    assert row["clean_cycles"] == 2
    assert row["fill_time_ms"] == 5 and row["refill_time_ms"] == 10
    assert row["hit"] is True


def test_档位没归零就不是冰山() -> None:
    """**这是本文件存在的理由。** 底下压着别人的 500 股，张三的 300 被吃掉、
    李四又挂 300——上一版算一轮，现在不算：档位从没空过，说不出补上来的是不是同一个人。"""
    o = _orders([1, 2, 20], [500, 300, 300], oids=[9, 1, 2])   # 500 一直挂着
    t = _fills([5, 25], [300, 300])
    got = _chains(o, t)
    assert got["hit"].to_list() == [False], "档位深度从未回到零"


def test_两个不相干的人同价同量不算冰山() -> None:
    """张三挂 300 被吃掉、档位归零；李四挂 500 又被吃掉；张三再挂 300。
    幅度不同把链打断——中间那个循环不是同一片。"""
    o = _orders([1, 10, 20], [300, 500, 300])
    t = _fills([5, 15, 25], [300, 500, 300])
    got = _chains(o, t).sort("chain")
    assert got["slice_vol"].to_list() == [300, 500, 300]
    assert got["hit"].to_list() == [False, False, False], "每条链都只有一个循环"


def test_一个不干净的循环让整条链不算() -> None:
    """保守方向：链按幅度切，不按干净与否切，否则不变量成了重言式、表里也没有分母。"""
    o = _orders([1, 10, 20], [300, 300, 300])
    t = pl.concat([_fills([5], [300]), _cancels([15], [300], [2]), _fills([25], [300])])
    row = _chains(o, t).row(0, named=True)
    assert row["n_cycles"] == 3 and row["clean_cycles"] == 2
    assert row["hit"] is False


def test_单个循环的链不是候选但留在表里当分母() -> None:
    got = _chains(_orders([1], [300]), _fills([5], [300])).row(0, named=True)
    assert got["n_cycles"] == 1 and got["refill_time_ms"] is None and got["hit"] is False


def test_一手的反复挂单不是切片的证据() -> None:
    """一手不能再切（2026-09-03 用户裁定）。"""
    o = _orders([1, 10], [100, 100])
    t = _fills([5, 15], [100, 100])
    row = _chains(o, t).row(0, named=True)
    assert row["clean_cycles"] == 2 and row["n_refills"] == 1, "机制成立"
    assert row["hit"] is False, "但一手不是一片"


def test_多轮数得出来() -> None:
    o = _orders([1, 10, 20, 30], [300] * 4)
    t = _fills([5, 15, 25, 35], [300] * 4)
    row = _chains(o, t).row(0, named=True)
    assert row["n_cycles"] == 4 and row["n_refills"] == 3 and row["hit"] is True
    assert row["total_filled"] == 1200


def test_不同价位各自成链() -> None:
    o = pl.concat([_orders([1, 10], [300, 300], oids=[1, 2], price=1000),
                   _orders([2, 11], [300, 300], oids=[3, 4], price=1100)])
    t = pl.concat([_fills([5, 15], [300, 300], price=1000),
                   _fills([6, 16], [300, 300], price=1100)])
    got = _chains(o, t)
    assert got.height == 2 and got["hit"].to_list() == [True, True]
