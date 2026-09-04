"""core.book.frames 的契约测试：帧间比对的三个角色各自能不能被单独判否。

隐藏深度这条的关键不在「成交 ≥ 展示量」，在**第三个角色**——两帧之间有没有新挂单。
三秒一帧里价格可以穿过该档再被新挂单撑住，那时「活着」的是新来的人，不是暗单
（红队 2026-09-03 架构严重 4）。本文件给这条一个真会红的反例。
"""

import polars as pl

from ftbv2.core.book import depth_deltas, frame_levels, frame_transitions
from ftbv2.core.registry import holds, spec

SYM = "000001.SZ"
_INV = spec("FillExceedsDisplayed").relation.invariants


def _quotes(rows: list[tuple[int, int, int]]) -> pl.DataFrame:
    """(q_time, bid1_px, bid1_sz) → 十档宽表；只填买一，其余档补 0。"""
    base = {f"{s}_{k}_{i}": [0] * len(rows) for s in ("ask", "bid") for k in ("px", "sz")
            for i in range(1, 11)}
    return pl.DataFrame({
        "symbol": [SYM] * len(rows), "time_ms": [r[0] for r in rows], **base,
    }).with_columns(bid_px_1=pl.Series([r[1] for r in rows]),
                    bid_sz_1=pl.Series([r[2] for r in rows]))


def _orders(times: list[int], vols: list[int], price: int = 1000) -> pl.DataFrame:
    n = len(times)
    return pl.DataFrame({"symbol": [SYM] * n, "side": ["B"] * n, "price": [price] * n,
                         "time_ms": times, "oid": list(range(1, n + 1)), "type": ["0"] * n,
                         "vol": vols},
                        schema={"symbol": pl.Utf8, "side": pl.Utf8, "price": pl.Int64,
                                "time_ms": pl.Int64, "oid": pl.Int64, "type": pl.Utf8,
                                "vol": pl.Int64})


def _trades(times: list[int], vols: list[int], price: int = 1000) -> pl.DataFrame:
    n = len(times)
    return pl.DataFrame({"symbol": [SYM] * n, "price": [price] * n, "time_ms": times,
                         "code": ["0"] * n, "bs": ["S"] * n, "vol": vols,
                         "ask_ref": [0] * n, "bid_ref": [0] * n},
                        schema={"symbol": pl.Utf8, "price": pl.Int64, "time_ms": pl.Int64,
                                "code": pl.Utf8, "bs": pl.Utf8, "vol": pl.Int64,
                                "ask_ref": pl.Int64, "bid_ref": pl.Int64})


def _run(quotes: pl.DataFrame, orders: pl.DataFrame, trades: pl.DataFrame) -> pl.DataFrame:
    t = frame_transitions(frame_levels(quotes), depth_deltas(orders, trades).deltas)
    return t.with_columns(holds(_INV, tuple(t.columns)).alias("hit"))


def test_帧序号按快照升序而不是按时间差() -> None:
    """帧间隔是数据说了算的，不是标称的 3 秒。"""
    lv = frame_levels(_quotes([(1000, 1000, 500), (9999, 1000, 300)]))
    assert lv.sort("frame")["frame"].to_list() == [0, 1]
    assert lv.sort("frame")["displayed_vol"].to_list() == [500, 300]


def test_档位被吃完全部展示量却还活着就是候选() -> None:
    q = _quotes([(1000, 1000, 500), (4000, 1000, 200)])
    got = _run(q, _orders([1], [500]), _trades([2000], [500]))
    row = got.filter(pl.col("frame") == 0).row(0, named=True)
    assert row["displayed_vol"] == 500 and row["executed_vol"] == 500
    assert row["surviving_vol"] == 200 and row["added_vol"] == 0
    assert row["hit"] is True


def test_两帧之间有新挂单时幸存的是新来的人() -> None:
    """**这条是本文件存在的理由。** 同样的展示量、同样的成交、同样幸存——
    只多了一笔两帧之间的新委托，就不该再叫隐藏深度。"""
    q = _quotes([(1000, 1000, 500), (4000, 1000, 200)])
    got = _run(q, _orders([1, 2500], [500, 200]), _trades([2000], [500]))
    row = got.filter(pl.col("frame") == 0).row(0, named=True)
    assert row["executed_vol"] == 500 and row["surviving_vol"] == 200
    assert row["added_vol"] == 200, "两帧之间的新挂单必须被数出来"
    assert row["hit"] is False


def test_成交不到展示量不是候选() -> None:
    q = _quotes([(1000, 1000, 500), (4000, 1000, 400)])
    got = _run(q, _orders([1], [500]), _trades([2000], [100]))
    assert got.filter(pl.col("frame") == 0).row(0, named=True)["hit"] is False


def test_档位没幸存不是候选() -> None:
    """被吃光就消失了——那只是一笔普通的把档位打穿，没有任何隐藏的证据。"""
    q = _quotes([(1000, 1000, 500), (4000, 1100, 300)])
    got = _run(q, _orders([1], [500]), _trades([2000], [500]))
    row = got.filter((pl.col("frame") == 0) & (pl.col("price") == 1000)).row(0, named=True)
    assert row["surviving_vol"] == 0 and row["hit"] is False


def test_最后一帧不产出行() -> None:
    """比不了就不是缺证据，是没有这一次比对。"""
    q = _quotes([(1000, 1000, 500), (4000, 1000, 200)])
    got = _run(q, _orders([1], [500]), _trades([2000], [500]))
    assert got["frame"].max() == 0


def test_帧间隔如实记录不是标称值() -> None:
    q = _quotes([(1000, 1000, 500), (7000, 1000, 200)])
    got = _run(q, _orders([1], [500]), _trades([2000], [500]))
    assert got.filter(pl.col("frame") == 0).row(0, named=True)["frame_gap_ms"] == 6000


def test_第一帧之前的成交不算进任何一段() -> None:
    """没有前帧可比的增量没有归属，不许被算进第一段里凑数。"""
    q = _quotes([(1000, 1000, 500), (4000, 1000, 500)])
    got = _run(q, _orders([1], [500]), _trades([500], [400]))
    assert got.filter(pl.col("frame") == 0).row(0, named=True)["executed_vol"] == 0
