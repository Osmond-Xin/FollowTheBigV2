"""core.book.refill 的契约测试：冰山的分界线是「补单在成交之后」，不是「同价同量相邻」。

上一版 `Seq_RepeatedSamePxVol` 只要求同价同量连着报两笔——密集报单里满地都是，
与是否成交无关。本文件给那种情形一个真会红的反例：它必须被判否。
"""

import polars as pl

from ftbv2.core.book import depth_deltas, order_fills, same_size_runs
from ftbv2.core.registry import holds, spec

SZ, SH = "000001.SZ", "600000.SH"
_INV = spec("RefillAfterFill").relation.invariants

_O = {"symbol": pl.Utf8, "side": pl.Utf8, "price": pl.Int64, "time_ms": pl.Int64,
      "oid": pl.Int64, "type": pl.Utf8, "vol": pl.Int64}
_T = {"symbol": pl.Utf8, "price": pl.Int64, "time_ms": pl.Int64, "code": pl.Utf8,
      "bs": pl.Utf8, "vol": pl.Int64, "ask_ref": pl.Int64, "bid_ref": pl.Int64}


def _orders(**cols: list) -> pl.DataFrame:
    return pl.DataFrame(cols, schema=_O)


def _trades(**cols: list) -> pl.DataFrame:
    return pl.DataFrame(cols, schema=_T) if cols else pl.DataFrame(schema=_T)


def _runs(orders: pl.DataFrame, trades: pl.DataFrame) -> pl.DataFrame:
    fills, _ = order_fills(depth_deltas(orders, trades).deltas)
    r = same_size_runs(fills)
    return r.with_columns(holds(_INV, tuple(r.columns)).alias("hit"))


def _buy(sym: str, times: list[int], oids: list[int], vols: list[int], price: int = 1000):
    n = len(times)
    return _orders(symbol=[sym] * n, side=["B"] * n, price=[price] * n, time_ms=times,
                   oid=oids, type=["0"] * n if sym.endswith(".SZ") else ["A"] * n, vol=vols)


def _hit_buy(sym: str, times: list[int], bid_refs: list[int], vols: list[int], price: int = 1000):
    """主动卖单吃掉买方挂单：被动方是买，所以关联号在 bid_ref。"""
    n = len(times)
    return _trades(symbol=[sym] * n, price=[price] * n, time_ms=times,
                   code=["0"] * n if sym.endswith(".SZ") else ["\x00"] * n,
                   bs=["S"] * n, vol=vols, ask_ref=[0] * n, bid_ref=bid_refs)


# ------------------------------------------------- 委托级成交进度

def test_耗尽按累计成交量判不按委托消失判() -> None:
    fills, _ = order_fills(depth_deltas(
        _buy(SZ, [1], [7], [500]), _hit_buy(SZ, [2, 3], [7, 7], [200, 300])).deltas)
    row = fills.row(0, named=True)
    assert row["filled_vol"] == 500 and row["exhausted"] is True
    assert row["t_exhaust"] == 3, "耗尽时刻是累计首次达到委托量的那一笔"


def test_没吃完的委托没有耗尽时刻() -> None:
    fills, _ = order_fills(depth_deltas(
        _buy(SZ, [1], [7], [500]), _hit_buy(SZ, [2], [7], [400])).deltas)
    row = fills.row(0, named=True)
    assert row["filled_vol"] == 400 and row["exhausted"] is False and row["t_exhaust"] is None


def test_一笔成交都没有的委托填零而不是缺失() -> None:
    fills, _ = order_fills(depth_deltas(_buy(SZ, [1], [7], [500]), _trades()).deltas)
    assert fills.row(0, named=True)["filled_vol"] == 0


# ------------------------------------------------- 冰山的分界线

def test_成交耗尽之后补单才是冰山() -> None:
    o = _buy(SZ, [1, 10], [7, 8], [500, 500])
    t = _hit_buy(SZ, [5], [7], [500])
    row = _runs(o, t).row(0, named=True)
    assert row["slice_vol"] == 500 and row["n_orders"] == 2
    assert row["fill_time_ms"] == 5 and row["refill_time_ms"] == 10
    assert row["n_refills"] == 1 and row["hit"] is True


def test_同价同量相邻两笔但一笔没成交不是冰山() -> None:
    """**这是本文件存在的理由。** 上一版切出来的就是这一类：密集报单里满地都是。"""
    row = _runs(_buy(SZ, [1, 10], [7, 8], [500, 500]), _trades()).row(0, named=True)
    assert row["n_orders"] == 2, "两笔同价同量确实连在一起"
    assert row["fill_time_ms"] is None and row["hit"] is False, "但没有成交，就不是冰山"


def test_补单发生在成交之前不是冰山() -> None:
    """错序必须被判否：先补后成交是两笔并排挂着，不是「吃一片补一片」。"""
    o = _buy(SZ, [1, 3], [7, 8], [500, 500])
    t = _hit_buy(SZ, [9], [7], [500])
    row = _runs(o, t).row(0, named=True)
    assert row["fill_time_ms"] == 9 and row["refill_time_ms"] is None
    assert row["hit"] is False


def test_换了量就是另一组() -> None:
    o = _buy(SZ, [1, 10, 20], [7, 8, 9], [500, 500, 300])
    got = _runs(o, _hit_buy(SZ, [5], [7], [500])).sort("run")
    assert got.height == 2
    assert got["slice_vol"].to_list() == [500, 300]
    assert got["hit"].to_list() == [True, False]


def test_多轮补单数得出来() -> None:
    o = _buy(SZ, [1, 10, 20], [7, 8, 9], [500, 500, 500])
    t = _hit_buy(SZ, [5, 15], [7, 8], [500, 500])
    row = _runs(o, t).row(0, named=True)
    assert row["n_orders"] == 3 and row["n_refills"] == 2 and row["hit"] is True
    assert row["total_filled"] == 1000


def test_不同价位各自成组() -> None:
    o = pl.concat([_buy(SZ, [1, 10], [7, 8], [500, 500], price=1000),
                   _buy(SZ, [2, 11], [17, 18], [500, 500], price=1100)])
    t = pl.concat([_hit_buy(SZ, [5], [7], [500], price=1000),
                   _hit_buy(SZ, [6], [17], [500], price=1100)])
    got = _runs(o, t)
    assert got.height == 2 and got["hit"].to_list() == [True, True]


# ------------------------------------------------- 关联率分交易所报

def test_关联率按交易所分开报不合成一个数() -> None:
    """上交所与深交所差得很远，合成一个数会把上交所的不可靠藏起来。"""
    o = pl.concat([_buy(SZ, [1], [7], [500]), _buy(SH, [1], [7], [500])])
    t = pl.concat([_hit_buy(SZ, [5], [7], [500]),
                   _hit_buy(SH, [5], [99], [500])])          # SH 这笔关联不上
    _, stats = order_fills(depth_deltas(o, t).deltas)
    assert stats.rate("SZ") == 1.0
    assert stats.rate("SH") == 0.0
    assert set(stats.by_exchange) == {"SZ", "SH"}


def test_没有成交行时关联率是零而分母看得见() -> None:
    _, stats = order_fills(depth_deltas(_buy(SZ, [1], [7], [500]), _trades()).deltas)
    assert stats.rate("SZ") == 0.0
    assert stats.by_exchange.get("SZ", {"traded_rows": 0})["traded_rows"] == 0
