"""core.book 的契约测试：两个交易所的深度口径、段的切法、缺口不得静默。
夹具是合成数据（纯核，不碰盘）；口径本身来自 20220104 实测，见 depth.py 模块 docstring。"""

import polars as pl
import pytest

from ftbv2.core.book import TICK, attach_touch, depth_deltas, level_episodes

_T_SCHEMA = {"symbol": pl.Utf8, "price": pl.Int64, "time_ms": pl.Int64,
             "code": pl.Utf8, "bs": pl.Utf8, "vol": pl.Int64, "ask_ref": pl.Int64, "bid_ref": pl.Int64}


def _orders(**cols: list) -> pl.DataFrame:
    return pl.DataFrame(cols, schema={"symbol": pl.Utf8, "side": pl.Utf8, "price": pl.Int64,
                                      "time_ms": pl.Int64, "oid": pl.Int64, "type": pl.Utf8, "vol": pl.Int64})


def _trades(**cols: list) -> pl.DataFrame:
    return pl.DataFrame(cols, schema=_T_SCHEMA) if cols else pl.DataFrame(schema=_T_SCHEMA)


def test_上交所撤单在orders且自带价量() -> None:
    o = _orders(symbol=["600000.SH"] * 2, side=["B"] * 2, price=[1000, 1000], time_ms=[1, 5],
                oid=[1, 1], type=["A", "D"], vol=[500, 500])
    d = depth_deltas(o, _trades()).deltas
    assert d["delta"].to_list() == [500, -500]
    assert d["reason"].to_list() == ["add", "cancel"]


def test_深交所撤单在trades且必须关联回orders取档位() -> None:
    """撤单行 price 恒为 0，只带被撤委托号——不关联就不知道撤在哪个档位。"""
    o = _orders(symbol=["000001.SZ"], side=["B"], price=[1000], time_ms=[1], oid=[77], type=["0"], vol=[500])
    t = _trades(symbol=["000001.SZ"], price=[0], time_ms=[5], code=["C"], bs=[" "], vol=[500],
                ask_ref=[0], bid_ref=[77])
    r = depth_deltas(o, t)
    assert r.deltas.filter(pl.col("reason") == "cancel")["price"].to_list() == [1000]
    assert r.unlinked_cancels == 0


def test_关联不上的撤单计数上报而不是静默丢弃() -> None:
    """「查不到 = 没有」被禁止。实测深交所命中率约 85%，丢掉的那部分必须看得见。"""
    o = _orders(symbol=["000001.SZ"], side=["B"], price=[1000], time_ms=[1], oid=[77], type=["0"], vol=[500])
    t = _trades(symbol=["000001.SZ"] * 2, price=[0, 0], time_ms=[5, 6], code=["C", "C"], bs=[" ", " "],
                vol=[500, 100], ask_ref=[0, 0], bid_ref=[77, 999])
    r = depth_deltas(o, t)
    assert r.unlinked_cancels == 1
    assert r.total_cancels == 2


def test_成交消耗的是被动方深度() -> None:
    """trades.bs 是主动方向，减的是它的反向。搞反了整张表的 side 都是错的。"""
    o = _orders(symbol=["600000.SH"], side=["S"], price=[1000], time_ms=[1], oid=[1], type=["A"], vol=[500])
    t = _trades(symbol=["600000.SH"], price=[1000], time_ms=[3], code=["\x00"], bs=["B"], vol=[200],
                ask_ref=[1], bid_ref=[2])
    d = depth_deltas(o, t).deltas
    assert d.filter(pl.col("reason") == "trade")["side"].to_list() == ["S"]


def test_同一档位堆起来再回零切成一段() -> None:
    o = _orders(symbol=["600000.SH"] * 4, side=["B"] * 4, price=[1000] * 4, time_ms=[1, 2, 9, 20],
                oid=[1, 2, 1, 2], type=["A", "A", "D", "D"], vol=[500, 300, 500, 300])
    ep = level_episodes(depth_deltas(o, _trades()).deltas)
    assert ep.height == 1
    r = ep.row(0, named=True)
    assert (r["peak_vol"], r["n_adds"], r["n_cancels"], r["executed_vol"], r["life_ms"]) == (800, 2, 2, 0, 19)


def test_回零后再堆起来是两段() -> None:
    o = _orders(symbol=["600000.SH"] * 4, side=["B"] * 4, price=[1000] * 4, time_ms=[1, 2, 10, 11],
                oid=[1, 1, 2, 2], type=["A", "D", "A", "D"], vol=[500, 500, 700, 700])
    ep = level_episodes(depth_deltas(o, _trades()).deltas)
    assert ep.height == 2
    assert ep["peak_vol"].to_list() == [500, 700]


def test_有成交的段与零成交的段分得开() -> None:
    """零成交是假墙候选的构成条件之一；有成交的档位消失是被吃掉的，不是被撤走的。"""
    o = _orders(symbol=["600000.SH"] * 2, side=["S"] * 2, price=[1000] * 2, time_ms=[1, 9],
                oid=[1, 1], type=["A", "D"], vol=[500, 300])
    t = _trades(symbol=["600000.SH"], price=[1000], time_ms=[5], code=["\x00"], bs=["B"], vol=[200],
                ask_ref=[1], bid_ref=[2])
    ep = level_episodes(depth_deltas(o, t).deltas)
    assert ep["executed_vol"].to_list() == [200]


def test_离最优价的距离按最近一帧算并记下帧龄() -> None:
    """快照 3 秒一帧，这是近似值——度量名与 frame_age_ms 都必须如实反映测量对象。"""
    ep = pl.DataFrame({"symbol": ["600000.SH"], "side": ["B"], "price": [1000 - 3 * TICK], "t_peak": [5000]})
    q = pl.DataFrame({"symbol": ["600000.SH"] * 2, "time_ms": [2000, 8000],
                      "ask1": [1100, 1100], "bid1": [1000, 1000]})
    out = attach_touch(ep, q).row(0, named=True)
    assert out["ticks_from_touch_at_nearest_frame"] == 3
    assert out["frame_age_ms"] == 3000


def test_峰值前没有任何快照时距离是缺失而不是零() -> None:
    ep = pl.DataFrame({"symbol": ["600000.SH"], "side": ["B"], "price": [1000], "t_peak": [100]})
    q = pl.DataFrame({"symbol": ["600000.SH"], "time_ms": [9000], "ask1": [1100], "bid1": [1000]})
    assert attach_touch(ep, q)["ticks_from_touch_at_nearest_frame"].to_list() == [None]


def test_tick取自PRICE_SCALE不另写字面量() -> None:
    from ftbv2.core.raw import PRICE_SCALE
    assert TICK == PRICE_SCALE // 100


@pytest.mark.parametrize("side,expected", [("B", "bid1"), ("S", "ask1")])
def test_买卖两侧各自比自己那一边的最优价(side: str, expected: str) -> None:
    ep = pl.DataFrame({"symbol": ["600000.SH"], "side": [side], "price": [1000], "t_peak": [5000]})
    q = pl.DataFrame({"symbol": ["600000.SH"], "time_ms": [2000], "ask1": [1000 + 2 * TICK], "bid1": [1000 - TICK]})
    touch = {"bid1": 1000 - TICK, "ask1": 1000 + 2 * TICK}[expected]
    assert attach_touch(ep, q)["ticks_from_touch_at_nearest_frame"].to_list() == [abs(1000 - touch) // TICK]


def test_收盘还挂着的档位不算消失() -> None:
    """「建起来又整个消失」的关键是回到零。收盘时仍挂着的档位只是还没结束，
    把它算成一段就等于把「墙还在」说成「墙没了」。合成夹具全都闭合时测不出这个，
    2026-09-03 在真实读取路径上跑出来才发现。"""
    o = _orders(symbol=["600000.SH"] * 2, side=["B"] * 2, price=[1000, 1000], time_ms=[1, 5],
                oid=[1, 2], type=["A", "A"], vol=[500, 300])
    ep = level_episodes(depth_deltas(o, _trades()).deltas)
    assert ep.height == 1
    assert ep["closed"].to_list() == [False]


def test_窗口前就有挂单的档位被排除在候选外但仍看得见() -> None:
    """只有成交、没有新增 ⇒ 深度为负 ⇒ 该档位在窗口开始前就有挂单。不是候选，也不静默丢。"""
    t = _trades(symbol=["600000.SH"], price=[1000], time_ms=[3], code=["\x00"], bs=["B"], vol=[200],
                ask_ref=[1], bid_ref=[2])
    o = _orders(symbol=[], side=[], price=[], time_ms=[], oid=[], type=[], vol=[])
    assert level_episodes(depth_deltas(o, t).deltas).height == 0


def test_盘口重建产出注册表不变量要读的每一列() -> None:
    """**这条把两个模块的耦合钉住。** 假墙的不变量声明它要读 closed / executed_vol / level / n_adds；
    那些列由 core.book 产出。core.book 改了列名而注册表没跟着改，判据就会在缺列上硬失败——
    与其等到跑真数据时才炸，不如在这里红。`core.book` 本身不 import 注册表（架构声明里没有这条边），
    对齐由本测试负责。"""
    from ftbv2.core.book import attach_visibility, quote_levels
    from ftbv2.core.registry import spec

    o = _orders(symbol=["000001.SZ"] * 2, side=["B"] * 2, price=[1000, 1000], time_ms=[1, 2],
                oid=[1, 2], type=["0", "0"], vol=[300, 200])
    t = pl.DataFrame({"symbol": ["000001.SZ"], "price": [0], "time_ms": [5], "code": ["C"],
                      "bs": [" "], "vol": [500], "ask_ref": [0], "bid_ref": [1]}, schema=_T_SCHEMA)
    quotes = pl.DataFrame({"symbol": ["000001.SZ"], "time_ms": [0], "ask1": [1100], "bid1": [1000],
                           **{f"{s}_px_{i}": [1000 if (s, i) == ("bid", 1) else 0]
                              for s in ("ask", "bid") for i in range(1, 11)}})
    episodes = attach_visibility(attach_touch(level_episodes(depth_deltas(o, t).deltas), quotes),
                                 quote_levels(quotes))
    needed = set(spec("LevelBuildThenVanish").relation.required_columns())
    assert needed <= set(episodes.columns), sorted(needed - set(episodes.columns))
