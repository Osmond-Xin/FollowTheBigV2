"""逐笔级日内微观结构特征（flow_day）。

读取 trades / orders 两个 stream，按 (日, 标的) 聚合产出每日一行 parquet。
用法：PYTHONPATH=src uv run python research/panel/flow_day.py --out /Volumes/xin/FollowTheBigV2-derived/flow [--workers 3] [--days ...]
产物：{out}/date=YYYYMMDD.parquet，每天一文件，幂等（已存在即跳过）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import multiprocessing as mp
import sys
import time
from pathlib import Path

import polars as pl

from ftbv2.core.raw import ReadRequest, parse_ledger, plan
from ftbv2.core.raw.schema import AM_END_MS, AM_START_MS, PM_END_MS, PM_START_MS
from ftbv2.io.raw import RawStore

ROOT = Path("/Volumes/辛的硬盘/data/preserve")
LEDGER = Path("ledger/defects.toml")

# ---- time constants (ms since midnight) ----
OPEN_CALL_START = (9 * 3600 + 25 * 60) * 1000        # 09:25:00.000
AM_START = AM_START_MS                               # 09:30:00.000
AM_END = AM_END_MS                                   # 11:30:00.000
PM_START = PM_START_MS                               # 13:00:00.000
PM_END = PM_END_MS                                   # 14:57:00.000
CLOSE_CALL_END = 15 * 3600 * 1000                    # 15:00:00.000
FIRST30_END = (10 * 3600) * 1000                     # 10:00:00.000
LAST30_START = (14 * 3600 + 27 * 60) * 1000          # 14:27:00.000
PRE_OPEN_START = (9 * 3600 + 15 * 60) * 1000         # 09:15:00.000
PRE_OPEN_PARTIAL_END = (9 * 3600 + 20 * 60) * 1000   # 09:20:00.000

TRADE_FIELDS = ("time_ms", "code", "bs", "price", "vol", "ask_ref", "bid_ref")
ORDER_FIELDS = ("time_ms", "oid", "type", "side", "price", "vol")

BUCKET_EDGES = (20_000.0, 50_000.0, 200_000.0, 1_000_000.0)
BUCKET_NAMES = ("b1", "b2", "b3", "b4", "b5")

PRICE_DIV = 10000.0
FIVE_MIN_MS = 5 * 60 * 1000


def _mkt(sym: pl.Expr) -> pl.Expr:
    return sym.str.slice(-2)


def _bucket_expr(amt_col: str) -> pl.Expr:
    """0..4 桶（b1..b5），按元。"""
    e = BUCKET_EDGES
    return (
        pl.when(pl.col(amt_col) < e[0]).then(0)
        .when(pl.col(amt_col) < e[1]).then(1)
        .when(pl.col(amt_col) < e[2]).then(2)
        .when(pl.col(amt_col) < e[3]).then(3)
        .otherwise(4)
    )


# ---- trades features ----

def features_trades(trades: pl.DataFrame) -> pl.DataFrame:  # noqa: PLR0915
    """trades: 一天的 trades stream（已带 day/symbol），输出 (day, symbol, 60+ 列)。"""
    t = trades.filter(pl.col("vol") > 0)
    t = t.with_columns(_mkt(pl.col("symbol")).alias("mkt"))
    is_sz = pl.col("mkt") == "SZ"
    is_fill = (pl.col("code") == "0") | (pl.col("code") == "\x00")
    is_sz_cancel = is_sz & (pl.col("code") == "C")
    t = t.with_columns(
        amt=(pl.col("price").cast(pl.Float64) / PRICE_DIV) * pl.col("vol").cast(pl.Float64),
        is_fill=is_fill,
        is_sz_cancel=is_sz_cancel,
    )

    tm = pl.col("time_ms")
    cont = (tm >= AM_START) & (tm < AM_END) | (tm >= PM_START) & (tm < PM_END)
    first30 = (tm >= AM_START) & (tm < FIRST30_END)
    last30 = (tm >= LAST30_START) & (tm < PM_END)
    open_call = (tm >= OPEN_CALL_START) & (tm < AM_START)
    close_call = tm >= PM_END          # 收盘竞价成交盖 15:00:00.xxx 时间戳，不设上界
    t = t.with_columns(
        cont=cont, first30=first30, last30=last30, open_call=open_call, close_call=close_call,
    )

    cf = t.filter(pl.col("cont") & pl.col("is_fill")).with_columns(
    agg_oid=pl.max_horizontal("ask_ref", "bid_ref"),
    pas_oid=pl.min_horizontal("ask_ref", "bid_ref"),
).sort("symbol", "time_ms")

    # ---- features 1, 2 ----（af = 连续竞价 + 两次集合竞价的成交；t_* 连续竞价口径，day_amt_c 全天口径）
    af = t.filter(pl.col("is_fill") & (pl.col("cont") | pl.col("open_call") | pl.col("close_call")))
    g = af.group_by("day", "symbol").agg(
        t_buy_amt=pl.col("amt").filter(pl.col("cont") & (pl.col("bs") == "B")).sum(),
        t_sell_amt=pl.col("amt").filter(pl.col("cont") & (pl.col("bs") == "S")).sum(),
        t_n_buy=(pl.col("cont") & (pl.col("bs") == "B")).sum().cast(pl.Int64),
        t_n_sell=(pl.col("cont") & (pl.col("bs") == "S")).sum().cast(pl.Int64),
        day_amt_c=pl.col("amt").sum(),
        amt_first30=pl.col("amt").filter(pl.col("first30")).sum(),
        amt_last30=pl.col("amt").filter(pl.col("last30")).sum(),
        amt_open_call=pl.col("amt").filter(pl.col("open_call")).sum(),
        amt_close_call=pl.col("amt").filter(pl.col("close_call")).sum(),
        buy_first30=pl.col("amt").filter(pl.col("first30") & (pl.col("bs") == "B")).sum(),
        sell_first30=pl.col("amt").filter(pl.col("first30") & (pl.col("bs") == "S")).sum(),
        buy_last30=pl.col("amt").filter(pl.col("last30") & (pl.col("bs") == "B")).sum(),
        sell_last30=pl.col("amt").filter(pl.col("last30") & (pl.col("bs") == "S")).sum(),
        buy_open_call=pl.col("amt").filter(pl.col("open_call") & (pl.col("bs") == "B")).sum(),
        sell_open_call=pl.col("amt").filter(pl.col("open_call") & (pl.col("bs") == "S")).sum(),
        buy_close_call=pl.col("amt").filter(pl.col("close_call") & (pl.col("bs") == "B")).sum(),
        sell_close_call=pl.col("amt").filter(pl.col("close_call") & (pl.col("bs") == "S")).sum(),
    ).with_columns(
        t_imb=(pl.col("t_buy_amt") - pl.col("t_sell_amt")) / (pl.col("t_buy_amt") + pl.col("t_sell_amt") + 1e-30),
        t_imb_first30=pl.when(pl.col("amt_first30") > 0).then(
            (pl.col("buy_first30") - pl.col("sell_first30")) / pl.col("amt_first30")
        ).otherwise(None),
        t_imb_last30=pl.when(pl.col("amt_last30") > 0).then(
            (pl.col("buy_last30") - pl.col("sell_last30")) / pl.col("amt_last30")
        ).otherwise(None),
        t_imb_open_call=pl.when(pl.col("amt_open_call") > 0).then(
            (pl.col("buy_open_call") - pl.col("sell_open_call")) / pl.col("amt_open_call")
        ).otherwise(None),
        t_imb_close_call=pl.when(pl.col("amt_close_call") > 0).then(
            (pl.col("buy_close_call") - pl.col("sell_close_call")) / pl.col("amt_close_call")
        ).otherwise(None),
        t_amt_share_first30=pl.col("amt_first30") / (pl.col("day_amt_c") + 1e-30),
        t_amt_share_last30=pl.col("amt_last30") / (pl.col("day_amt_c") + 1e-30),
        t_amt_share_open_call=pl.col("amt_open_call") / (pl.col("day_amt_c") + 1e-30),
        t_amt_share_close_call=pl.col("amt_close_call") / (pl.col("day_amt_c") + 1e-30),
    ).drop(
        "amt_first30", "amt_last30", "amt_open_call", "amt_close_call",
        "buy_first30", "sell_first30", "buy_last30", "sell_last30",
        "buy_open_call", "sell_open_call", "buy_close_call", "sell_close_call",
        "day_amt_c",
    )

    # ---- feature 8: SZ cancels ----
    sz_cancels = t.filter(pl.col("is_sz_cancel")).with_columns(
        is_bid_cancel=pl.col("bid_ref") > 0,
        is_ask_cancel=pl.col("ask_ref") > 0,
    )
    gc = sz_cancels.group_by("day", "symbol").agg(
        c_n_bid=pl.col("is_bid_cancel").sum().cast(pl.Int64),
        c_n_ask=pl.col("is_ask_cancel").sum().cast(pl.Int64),
        c_vol_bid=pl.col("vol").filter(pl.col("is_bid_cancel")).sum(),
        c_vol_ask=pl.col("vol").filter(pl.col("is_ask_cancel")).sum(),
    )
    g = g.join(gc, on=["day", "symbol"], how="left").with_columns(
        pl.col("c_n_bid").fill_null(0),
        pl.col("c_n_ask").fill_null(0),
        pl.col("c_vol_bid").fill_null(0.0),
        pl.col("c_vol_ask").fill_null(0.0),
    )

    # ---- feature 3: order-level (aggressor / passive) bucket aggregation ----
    fills = cf.with_columns(
        agg_amt=pl.col("amt"),
        agg_vol=pl.col("vol").cast(pl.Float64),
        agg_buy=pl.col("bs") == "B",
        agg_sell=pl.col("bs") == "S",
    )

    # 主动方：按 (symbol, 主动方委托号) 聚合
    agg_per = fills.group_by("day", "symbol", "agg_oid").agg(
        a_amt=pl.col("agg_amt").sum(),
        a_vol=pl.col("agg_vol").sum(),
        a_buy_amt=pl.col("agg_amt").filter(pl.col("agg_buy")).sum(),
        a_sell_amt=pl.col("agg_amt").filter(pl.col("agg_sell")).sum(),
    )
    a_buck = (
        agg_per.with_columns(bk=_bucket_expr("a_amt"))
        .group_by("day", "symbol", "bk")
        .agg(
            a_bk_amt=pl.col("a_amt").sum(),
            a_bk_buy=pl.col("a_buy_amt").sum(),
            a_bk_sell=pl.col("a_sell_amt").sum(),
        )
    )

    # passive: per (symbol, pas_oid). passive buy ← bs=S, passive sell ← bs=B
    pas_per = fills.group_by("day", "symbol", "pas_oid").agg(
        p_amt=pl.col("agg_amt").sum(),
        p_vol=pl.col("agg_vol").sum(),
        p_buy_amt=pl.col("agg_amt").filter(pl.col("agg_sell")).sum(),
        p_sell_amt=pl.col("agg_amt").filter(pl.col("agg_buy")).sum(),
    )
    p_buck = (
        pas_per.with_columns(bk=_bucket_expr("p_amt"))
        .group_by("day", "symbol", "bk")
        .agg(
            p_bk_amt=pl.col("p_amt").sum(),
            p_bk_buy=pl.col("p_buy_amt").sum(),
            p_bk_sell=pl.col("p_sell_amt").sum(),
        )
    )

    # ---- feature 4: row-level (classic) ----
    r_buck = (
        cf.with_columns(bk=_bucket_expr("amt"))
        .group_by("day", "symbol", "bk")
        .agg(
            r_bk_amt=pl.col("amt").sum(),
            r_bk_buy=pl.col("amt").filter(pl.col("bs") == "B").sum(),
            r_bk_sell=pl.col("amt").filter(pl.col("bs") == "S").sum(),
        )
    )

    # ---- feature 5: aggressor-order fingerprints ----
    fp = agg_per.with_columns(
        round500=(pl.col("a_vol") % 500 == 0) & (pl.col("a_vol") >= 500),
        round1000=(pl.col("a_vol") % 1000 == 0) & (pl.col("a_vol") >= 1000),
        lot1=pl.col("a_vol") == 100,
    ).group_by("day", "symbol").agg(
        fp_amt=pl.col("a_amt").sum(),
        fp_round500_amt=pl.col("a_amt").filter(pl.col("round500")).sum(),
        fp_round1000_amt=pl.col("a_amt").filter(pl.col("round1000")).sum(),
        fp_n=pl.len().cast(pl.Int64),
        fp_lot1_n=pl.col("lot1").sum().cast(pl.Int64),
    ).with_columns(
        f_round500=pl.col("fp_round500_amt") / (pl.col("fp_amt") + 1e-30),
        f_round1000=pl.col("fp_round1000_amt") / (pl.col("fp_amt") + 1e-30),
        f_lot1=pl.col("fp_lot1_n").cast(pl.Float64) / (pl.col("fp_n") + 1e-30),
    ).select("day", "symbol", "f_round500", "f_round1000", "f_lot1")

    # pivot bucket tables → wide
    def _pivot(df: pl.DataFrame, prefix: str, buy: str, sell: str, amt: str) -> pl.DataFrame:
        out = df.select("day", "symbol", "bk", amt, buy, sell).pivot(
            on="bk", index=["day", "symbol"], values=[amt, buy, sell], aggregate_function="sum",
        )
        rename = {}
        for k in range(5):
            rename[f"{amt}_{k}"] = f"{prefix}_amt_{BUCKET_NAMES[k]}"
            rename[f"{buy}_{k}"] = f"{prefix}_buy_{BUCKET_NAMES[k]}"
            rename[f"{sell}_{k}"] = f"{prefix}_sell_{BUCKET_NAMES[k]}"
        return out.rename(rename).fill_null(0.0)

    a_w = _pivot(a_buck, "o", "a_bk_buy", "a_bk_sell", "a_bk_amt")
    p_w = _pivot(p_buck, "p", "p_bk_buy", "p_bk_sell", "p_bk_amt")
    r_w = _pivot(r_buck, "r", "r_bk_buy", "r_bk_sell", "r_bk_amt")

    # add net_k = (buy_k - sell_k) / day_amt_c, share_k = amt_k / day_amt_c
    # day_amt_c is currently in `g` (before drop). Let's recompute from these bucket sums.
    def _add_net_share(w: pl.DataFrame, prefix: str) -> pl.DataFrame:
        amt_cols = [f"{prefix}_amt_{n}" for n in BUCKET_NAMES]
        denom_expr = sum(pl.col(c) for c in amt_cols) + 1e-30
        new_cols = []
        for n in BUCKET_NAMES:
            buy = pl.col(f"{prefix}_buy_{n}")
            sell = pl.col(f"{prefix}_sell_{n}")
            amt = pl.col(f"{prefix}_amt_{n}")
            new_cols.append(((buy - sell) / denom_expr).alias(f"{prefix}_net_{n}"))
            new_cols.append((amt / denom_expr).alias(f"{prefix}_share_{n}"))
        # drop buy/sell columns (kept internally); we keep only net/share + amt for record
        drop = [f"{prefix}_buy_{n}" for n in BUCKET_NAMES] + [f"{prefix}_sell_{n}" for n in BUCKET_NAMES]
        return w.with_columns(new_cols).drop(drop)

    a_w = _add_net_share(a_w, "o")
    p_w = _add_net_share(p_w, "p")
    r_w = _add_net_share(r_w, "r")

    out = (
        g.join(a_w, on=["day", "symbol"], how="left")
         .join(p_w, on=["day", "symbol"], how="left")
         .join(r_w, on=["day", "symbol"], how="left")
         .join(fp, on=["day", "symbol"], how="left")
    )

    # ---- feature 6: trade-sign autocorrelation & mean run length ----
    # cf is already sorted by (symbol, time_ms); ts derives from it.
    ts = (
        cf.select("day", "symbol", "time_ms",
                  sign=pl.when(pl.col("bs") == "B").then(1).when(pl.col("bs") == "S").then(-1).otherwise(0))
        .with_columns(
            prev_sign=pl.col("sign").shift(1).over("symbol"),
            prod=pl.col("sign") * pl.col("sign").shift(1).over("symbol"),
            run_id=pl.col("sign").rle_id().over("symbol"),
        )
    )
    ac = (
        ts.group_by("day", "symbol")
        .agg(
            t_n_sign=pl.col("sign").abs().sum().cast(pl.Int64),
            t_sign_prod=pl.col("prod").sum(),
            t_sign_sq=pl.col("prev_sign").abs().sum(),   # = N-1 non-null prev_sign
            t_n_runs=pl.col("run_id").max(),
        )
        .with_columns(
            t_sign_ac1=pl.when(pl.col("t_sign_sq") > 0).then(
                pl.col("t_sign_prod") / pl.col("t_sign_sq")
            ).otherwise(None),
            t_run_mean=pl.when(pl.col("t_n_runs") > 0).then(
                pl.col("t_n_sign").cast(pl.Float64) / pl.col("t_n_runs").cast(pl.Float64)
            ).otherwise(None),
        )
        .select("day", "symbol", "t_sign_ac1", "t_run_mean")
    )
    out = out.join(ac, on=["day", "symbol"], how="left")

    # ---- feature 7: 5-min binned features ----
    # cf is already sorted by (symbol, time_ms). Add bin index, group_by.
    cf_b = cf.select("day", "symbol", "time_ms", "bs", "price", "amt").with_columns(
        bin=(pl.col("time_ms") // FIVE_MIN_MS).cast(pl.Int64),
    )
    binned = (
        cf_b.group_by("day", "symbol", "bin")
        .agg(
            px=pl.col("price").last(),
            buy_amt=pl.col("amt").filter(pl.col("bs") == "B").sum(),
            sell_amt=pl.col("amt").filter(pl.col("bs") == "S").sum(),
        )
        .sort("day", "symbol", "bin")
        .with_columns(
            prev_px=pl.col("px").shift(1).over("symbol"),
        )
        .filter(pl.col("prev_px").is_not_null() & (pl.col("prev_px") > 0))
        .with_columns(
            ret=(pl.col("px").cast(pl.Float64) / pl.col("prev_px").cast(pl.Float64)).log(),
            signed_amt_w=(pl.col("buy_amt") - pl.col("sell_amt")) / 10_000.0,
        )
    )
    moments = binned.group_by("day", "symbol").agg(
        n_ret=pl.len().cast(pl.Int64),
        sum_sq=(pl.col("ret") * pl.col("ret")).sum(),
        sum_x=(pl.col("signed_amt_w")).sum(),
        sum_y=pl.col("ret").sum(),
        sum_xx=(pl.col("signed_amt_w") * pl.col("signed_amt_w")).sum(),
        sum_xy=(pl.col("signed_amt_w") * pl.col("ret")).sum(),
        skew=pl.col("ret").skew(),
        kurt=pl.col("ret").kurtosis(),
        rv=pl.col("ret").pow(2).sum().sqrt(),
        rskew=pl.col("ret").skew(),
        rkurt=pl.col("ret").kurtosis(),
    ).with_columns(
        kyle_lambda=pl.when(pl.col("sum_xx") > 0).then(
            pl.col("sum_xy") / pl.col("sum_xx")
        ).otherwise(None),
    ).select("day", "symbol", "rv", "rskew", "rkurt", "kyle_lambda")

    # amihud = |last/first - 1| / day_amt; cf already sorted by (symbol, time_ms)
    fl = (
        cf.group_by("day", "symbol")
        .agg(
            f_px=pl.col("price").first(),
            l_px=pl.col("price").last(),
            day_amt=pl.col("amt").sum(),
        )
        .with_columns(
            amihud=((pl.col("l_px").cast(pl.Float64) / pl.col("f_px").cast(pl.Float64)) - 1.0).abs()
                   / (pl.col("day_amt") + 1e-30),
        )
        .select("day", "symbol", "amihud")
    )
    out = out.join(moments, on=["day", "symbol"], how="left").join(fl, on=["day", "symbol"], how="left")
    return out.sort("symbol")


# ---- orders features ----

def features_orders(orders: pl.DataFrame, cancels: pl.DataFrame) -> pl.DataFrame:
    """orders: 当日 orders stream（含 day/symbol）。
    cancels: 撤单流 (day, symbol, side, oid, cancel_time_ms, cancel_vol)，SZ 由 trades 派生、SH 由 orders 派生。
    """
    o = orders.filter(pl.col("vol") > 0).with_columns(_mkt(pl.col("symbol")).alias("mkt"))
    is_sz = pl.col("mkt") == "SZ"
    is_real_sz = is_sz & pl.col("type").is_in(["0", "1", "U"]) & pl.col("side").is_in(["B", "S"])
    is_real_sh = (pl.col("mkt") == "SH") & (pl.col("type") == "A") & pl.col("side").is_in(["B", "S"])
    real = o.filter(is_real_sz | is_real_sh).with_columns(
        ord_amt=(pl.col("price").cast(pl.Float64) / PRICE_DIV) * pl.col("vol").cast(pl.Float64),
    )

    tm = pl.col("time_ms")
    cont = (tm >= AM_START) & (tm < AM_END) | (tm >= PM_START) & (tm < PM_END)
    real = real.with_columns(cont=cont)
    cont_orders = real.filter(pl.col("cont"))

    # ---- feature 9 ----
    g9 = (
        cont_orders.group_by("day", "symbol")
        .agg(
            q_n_b=pl.col("side").eq("B").sum().cast(pl.Int64),
            q_n_s=pl.col("side").eq("S").sum().cast(pl.Int64),
            q_amt_b=pl.col("ord_amt").filter(pl.col("side") == "B").sum(),
            q_amt_s=pl.col("ord_amt").filter(pl.col("side") == "S").sum(),
            q_mean_amt_b=pl.when(pl.col("side").eq("B").sum() > 0).then(
                pl.col("ord_amt").filter(pl.col("side") == "B").mean()
            ).otherwise(None),
            q_mean_amt_s=pl.when(pl.col("side").eq("S").sum() > 0).then(
                pl.col("ord_amt").filter(pl.col("side") == "S").mean()
            ).otherwise(None),
            q_big20_amt_b=pl.col("ord_amt").filter((pl.col("side") == "B") & (pl.col("ord_amt") >= 200_000)).sum(),
            q_big100_amt_b=pl.col("ord_amt").filter((pl.col("side") == "B") & (pl.col("ord_amt") >= 1_000_000)).sum(),
            q_big20_amt_s=pl.col("ord_amt").filter((pl.col("side") == "S") & (pl.col("ord_amt") >= 200_000)).sum(),
            q_big100_amt_s=pl.col("ord_amt").filter((pl.col("side") == "S") & (pl.col("ord_amt") >= 1_000_000)).sum(),
        )
        .with_columns(
            q_big20_share_b=pl.col("q_big20_amt_b") / (pl.col("q_amt_b") + 1e-30),
            q_big100_share_b=pl.col("q_big100_amt_b") / (pl.col("q_amt_b") + 1e-30),
            q_big20_share_s=pl.col("q_big20_amt_s") / (pl.col("q_amt_s") + 1e-30),
            q_big100_share_s=pl.col("q_big100_amt_s") / (pl.col("q_amt_s") + 1e-30),
        )
        .drop("q_big20_amt_b", "q_big100_amt_b", "q_big20_amt_s", "q_big100_amt_s")
    )

    # ---- feature 12 ----
    fp = (
        cont_orders.with_columns(
            ord_round500=(pl.col("vol") % 500 == 0) & (pl.col("vol") >= 500),
            ord_lot1=pl.col("vol") == 100,
        )
        .group_by("day", "symbol")
        .agg(
            qf_round500_amt_b=pl.col("ord_amt").filter(pl.col("ord_round500") & (pl.col("side") == "B")).sum(),
            qf_round500_amt_s=pl.col("ord_amt").filter(pl.col("ord_round500") & (pl.col("side") == "S")).sum(),
            qf_lot1_n_b=pl.col("ord_lot1").filter(pl.col("side") == "B").sum().cast(pl.Int64),
            qf_lot1_n_s=pl.col("ord_lot1").filter(pl.col("side") == "S").sum().cast(pl.Int64),
            qf_n_b=(pl.col("side") == "B").sum().cast(pl.Int64),
            qf_n_s=(pl.col("side") == "S").sum().cast(pl.Int64),
            qf_amt_b=pl.col("ord_amt").filter(pl.col("side") == "B").sum(),
            qf_amt_s=pl.col("ord_amt").filter(pl.col("side") == "S").sum(),
        )
        .with_columns(
            qf_round500_b=pl.col("qf_round500_amt_b") / (pl.col("qf_amt_b") + 1e-30),
            qf_round500_s=pl.col("qf_round500_amt_s") / (pl.col("qf_amt_s") + 1e-30),
            qf_lot1_b=pl.col("qf_lot1_n_b").cast(pl.Float64) / (pl.col("qf_n_b") + 1e-30),
            qf_lot1_s=pl.col("qf_lot1_n_s").cast(pl.Float64) / (pl.col("qf_n_s") + 1e-30),
        )
        .drop("qf_round500_amt_b", "qf_round500_amt_s", "qf_lot1_n_b", "qf_lot1_n_s",
              "qf_n_b", "qf_n_s", "qf_amt_b", "qf_amt_s")
    )

    # ---- feature 10: cancels joined to orders ----
    placed = real.select(
        "day", "symbol", "oid", "side",
        place_time_ms=pl.col("time_ms"),
        place_vol=pl.col("vol").cast(pl.Float64),
        place_amt=pl.col("ord_amt"),
    )

    if cancels.height > 0:
        joined = placed.join(
            cancels.select("day", "symbol", "oid", "cancel_time_ms",
                           cancel_vol=pl.col("cancel_vol").cast(pl.Float64)),
            on=["day", "symbol", "oid"], how="inner",
        ).with_columns(
            life_ms=pl.col("cancel_time_ms") - pl.col("place_time_ms"),
        )
    else:
        joined = placed.head(0).with_columns(
            life_ms=pl.Series("life_ms", [], dtype=pl.Int64)
        )

    # placed aggregates
    placed_agg = placed.with_columns(is_big20=pl.col("place_amt") >= 200_000).group_by("day", "symbol").agg(
        placed_n_b=pl.col("side").eq("B").sum().cast(pl.Int64),
        placed_n_s=pl.col("side").eq("S").sum().cast(pl.Int64),
        placed_amt_b=pl.col("place_amt").filter(pl.col("side") == "B").sum(),
        placed_amt_s=pl.col("place_amt").filter(pl.col("side") == "S").sum(),
        placed_big20_n_b=pl.col("is_big20").filter(pl.col("side") == "B").sum().cast(pl.Int64),
        placed_big20_n_s=pl.col("is_big20").filter(pl.col("side") == "S").sum().cast(pl.Int64),
    )

    if joined.height > 0:
        cx_agg = joined.with_columns(is_big20=pl.col("place_amt") >= 200_000).group_by("day", "symbol").agg(
            cx_n_b=pl.col("side").eq("B").sum().cast(pl.Int64),
            cx_n_s=pl.col("side").eq("S").sum().cast(pl.Int64),
            cx_amt_b=(pl.col("cancel_vol") * pl.col("place_amt") / pl.col("place_vol")).filter(pl.col("side") == "B").sum(),
            cx_amt_s=(pl.col("cancel_vol") * pl.col("place_amt") / pl.col("place_vol")).filter(pl.col("side") == "S").sum(),
            cx_hf_n_b=pl.col("life_ms").filter((pl.col("side") == "B") & (pl.col("life_ms") < 1000)).count().cast(pl.Int64),
            cx_hf_n_s=pl.col("life_ms").filter((pl.col("side") == "S") & (pl.col("life_ms") < 1000)).count().cast(pl.Int64),
            cx_big20_n_b=pl.col("is_big20").filter(pl.col("side") == "B").sum().cast(pl.Int64),
            cx_big20_n_s=pl.col("is_big20").filter(pl.col("side") == "S").sum().cast(pl.Int64),
            q_life_ms_b=pl.col("life_ms").filter(pl.col("side") == "B").median(),
            q_life_ms_s=pl.col("life_ms").filter(pl.col("side") == "S").median(),
        )
    else:
        cx_agg = placed.head(0).select(
            "day", "symbol",
            cx_n_b=pl.lit(0, dtype=pl.Int64),
            cx_n_s=pl.lit(0, dtype=pl.Int64),
            cx_amt_b=pl.lit(0.0),
            cx_amt_s=pl.lit(0.0),
            cx_hf_n_b=pl.lit(0, dtype=pl.Int64),
            cx_hf_n_s=pl.lit(0, dtype=pl.Int64),
            cx_big20_n_b=pl.lit(0, dtype=pl.Int64),
            cx_big20_n_s=pl.lit(0, dtype=pl.Int64),
            q_life_ms_b=pl.lit(None, dtype=pl.Int64),
            q_life_ms_s=pl.lit(None, dtype=pl.Int64),
        )

    g10 = (
        placed_agg.join(cx_agg, on=["day", "symbol"], how="left")
        .with_columns(
            cx_rate_b=pl.col("cx_n_b").cast(pl.Float64) / (pl.col("placed_n_b") + 1e-30),
            cx_rate_s=pl.col("cx_n_s").cast(pl.Float64) / (pl.col("placed_n_s") + 1e-30),
            cx_hf_b=pl.col("cx_hf_n_b").cast(pl.Float64) / (pl.col("placed_n_b") + 1e-30),
            cx_hf_s=pl.col("cx_hf_n_s").cast(pl.Float64) / (pl.col("placed_n_s") + 1e-30),
            cx_amt_rate_b=pl.col("cx_amt_b") / (pl.col("placed_amt_b") + 1e-30),
            cx_amt_rate_s=pl.col("cx_amt_s") / (pl.col("placed_amt_s") + 1e-30),
            cx_big_rate_b=pl.col("cx_big20_n_b").cast(pl.Float64) / (pl.col("placed_big20_n_b") + 1e-30),
            cx_big_rate_s=pl.col("cx_big20_n_s").cast(pl.Float64) / (pl.col("placed_big20_n_s") + 1e-30),
            q_life_med_b=pl.col("q_life_ms_b"),
            q_life_med_s=pl.col("q_life_ms_s"),
        )
        .drop("cx_n_b", "cx_n_s", "cx_amt_b", "cx_amt_s",
              "cx_hf_n_b", "cx_hf_n_s", "cx_big20_n_b", "cx_big20_n_s",
              "placed_big20_n_b", "placed_big20_n_s",
              "placed_amt_b", "placed_amt_s", "placed_n_b", "placed_n_s",
              "q_life_ms_b", "q_life_ms_s")
    )

    # ---- feature 11: pre-open ----
    pre = real.filter(
        (pl.col("time_ms") >= PRE_OPEN_START) & (pl.col("time_ms") < AM_START)
    ).with_columns(
        in_partial=(pl.col("time_ms") < PRE_OPEN_PARTIAL_END),
    )
    pre_agg = pre.group_by("day", "symbol").agg(
        pre_n_b=pl.col("side").eq("B").sum().cast(pl.Int64),
        pre_n_s=pl.col("side").eq("S").sum().cast(pl.Int64),
        pre_amt_b=pl.col("ord_amt").filter(pl.col("side") == "B").sum(),
        pre_amt_s=pl.col("ord_amt").filter(pl.col("side") == "S").sum(),
        pre_partial_n_b=pl.col("in_partial").filter(pl.col("side") == "B").sum().cast(pl.Int64),
        pre_partial_n_s=pl.col("in_partial").filter(pl.col("side") == "S").sum().cast(pl.Int64),
    )

    if cancels.height > 0:
        pre_cancels = (
            cancels.filter((pl.col("cancel_time_ms") >= PRE_OPEN_START) & (pl.col("cancel_time_ms") < OPEN_CALL_START))
            .join(placed.select("day", "symbol", "oid", "side",
                                place_in_partial=pl.col("place_time_ms") < PRE_OPEN_PARTIAL_END),
                  on=["day", "symbol", "oid"], how="inner")
        )
        if pre_cancels.height > 0:
            pc = pre_cancels.group_by("day", "symbol").agg(
                pre_cx_partial_n_b=pl.col("place_in_partial").filter(pl.col("side") == "B").sum().cast(pl.Int64),
                pre_cx_partial_n_s=pl.col("place_in_partial").filter(pl.col("side") == "S").sum().cast(pl.Int64),
            )
            pre_agg = pre_agg.join(pc, on=["day", "symbol"], how="left")
        else:
            pre_agg = pre_agg.with_columns(
                pre_cx_partial_n_b=pl.lit(0, dtype=pl.Int64),
                pre_cx_partial_n_s=pl.lit(0, dtype=pl.Int64),
            )
    else:
        pre_agg = pre_agg.with_columns(
            pre_cx_partial_n_b=pl.lit(0, dtype=pl.Int64),
            pre_cx_partial_n_s=pl.lit(0, dtype=pl.Int64),
        )

    g11 = pre_agg.with_columns(
        pre_cx_b=pl.col("pre_cx_partial_n_b").cast(pl.Float64) / (pl.col("pre_partial_n_b") + 1e-30),
        pre_cx_s=pl.col("pre_cx_partial_n_s").cast(pl.Float64) / (pl.col("pre_partial_n_s") + 1e-30),
    ).drop("pre_cx_partial_n_b", "pre_cx_partial_n_s")

    out = (
        g9.join(fp, on=["day", "symbol"], how="full", coalesce=True)
          .join(g10, on=["day", "symbol"], how="full", coalesce=True)
          .join(g11, on=["day", "symbol"], how="full", coalesce=True)
    )
    return out.sort("symbol")


# ---- per-day worker ----

def _read_trades(st: RawStore, day: dt.date) -> pl.DataFrame:
    cat = st.catalog("trades", (day,))
    if not cat.files:
        return pl.DataFrame()
    res = st.execute(plan(ReadRequest("trades", (day,), TRADE_FIELDS), cat, parse_ledger(LEDGER.read_text(encoding="utf-8"))))
    return res.frame


def _read_orders(st: RawStore, day: dt.date) -> pl.DataFrame:
    cat = st.catalog("orders", (day,))
    if not cat.files:
        return pl.DataFrame()
    res = st.execute(plan(ReadRequest("orders", (day,), ORDER_FIELDS), cat, parse_ledger(LEDGER.read_text(encoding="utf-8"))))
    return res.frame


def _sz_cancels(st: RawStore, day: dt.date) -> pl.DataFrame:
    cat = st.catalog("trades", (day,))
    if not cat.files:
        return pl.DataFrame()
    res = st.execute(plan(ReadRequest("trades", (day,), ("time_ms", "code", "vol", "ask_ref", "bid_ref")), cat,
                            parse_ledger(LEDGER.read_text(encoding="utf-8"))))
    df = res.frame.filter((pl.col("code") == "C") & (pl.col("vol") > 0))
    buys = df.filter(pl.col("bid_ref") > 0).select(
        "symbol", side=pl.lit("B"),
        cancel_time_ms=pl.col("time_ms"),
        oid=pl.col("bid_ref"),
        cancel_vol=pl.col("vol"),
    )
    sells = df.filter(pl.col("ask_ref") > 0).select(
        "symbol", side=pl.lit("S"),
        cancel_time_ms=pl.col("time_ms"),
        oid=pl.col("ask_ref"),
        cancel_vol=pl.col("vol"),
    )
    if buys.height + sells.height == 0:
        return pl.DataFrame()
    out = pl.concat([buys, sells], how="vertical")
    return out.with_columns(pl.lit(day).cast(pl.Date).alias("day")).select(
        "day", "symbol", "side", "oid", "cancel_time_ms", "cancel_vol",
    )


def _sh_cancels(orders: pl.DataFrame) -> pl.DataFrame:
    """SH cancels: orders.stream 中 type='D' 行。"""
    df = orders.filter(pl.col("type") == "D").select(
        "day", "symbol", "side",
        cancel_time_ms=pl.col("time_ms"),
        oid=pl.col("oid"),
        cancel_vol=pl.col("vol"),
    )
    return df


def build_day(args: tuple[dt.date, Path]) -> str:
    day, out = args
    path = out / f"date={day:%Y%m%d}.parquet"
    if path.exists():
        return f"{day} skip"
    t0 = time.time()
    ledger = parse_ledger(LEDGER.read_text(encoding="utf-8"))
    st = RawStore(ROOT, ledger)

    # ---- trades: read once, derive features & SZ cancels ----
    cat_t = st.catalog("trades", (day,))
    if not cat_t.files:
        return f"{day} NO_TRADES gaps={cat_t.missing_days}"
    res_t = st.execute(plan(ReadRequest("trades", (day,), TRADE_FIELDS), cat_t, ledger))
    trades = res_t.frame
    t_read_t = time.time()
    if trades.height == 0:
        return f"{day} EMPTY_TRADES"

    # SZ cancels from same trades df (cheap, no second read)
    sz_cancels_raw = trades.filter((pl.col("code") == "C") & (pl.col("vol") > 0))
    sz_cx = pl.concat([
        sz_cancels_raw.filter(pl.col("bid_ref") > 0).select(
            "day", "symbol", side=pl.lit("B"),
            cancel_time_ms=pl.col("time_ms"),
            oid=pl.col("bid_ref"),
            cancel_vol=pl.col("vol"),
        ),
        sz_cancels_raw.filter(pl.col("ask_ref") > 0).select(
            "day", "symbol", side=pl.lit("S"),
            cancel_time_ms=pl.col("time_ms"),
            oid=pl.col("ask_ref"),
            cancel_vol=pl.col("vol"),
        ),
    ], how="vertical") if sz_cancels_raw.height > 0 else pl.DataFrame()

    ft = features_trades(trades)
    del trades
    t_ft = time.time()

    # ---- orders: read once, derive features & SH cancels ----
    cat_o = st.catalog("orders", (day,))
    if not cat_o.files:
        return f"{day} NO_ORDERS"
    res_o = st.execute(plan(ReadRequest("orders", (day,), ORDER_FIELDS), cat_o, ledger))
    orders = res_o.frame
    t_read_o = time.time()
    if orders.height == 0:
        return f"{day} EMPTY_ORDERS"

    sh_cx = _sh_cancels(orders)

    cancels = pl.concat([sz_cx, sh_cx], how="vertical") if (sz_cx.height + sh_cx.height) > 0 else pl.DataFrame(
        schema={"day": pl.Date, "symbol": pl.String, "side": pl.String,
                "oid": pl.Int64, "cancel_time_ms": pl.Int64, "cancel_vol": pl.Int64}
    )

    fo = features_orders(orders, cancels)
    del orders
    t_fo = time.time()

    # ---- join ----
    out_df = (
        ft.join(fo, on=["day", "symbol"], how="full", coalesce=True)
          .with_columns(mkt=pl.col("symbol").str.slice(-2))
          .sort("symbol")
    )

    tmp = path.with_suffix(".tmp")
    out_df.write_parquet(tmp)
    tmp.rename(path)
    return (
        f"{day} read_t={t_read_t - t0:.1f}s ft={t_ft - t_read_t:.1f}s "
        f"read_o={t_read_o - t_ft:.1f}s fo={t_fo - t_read_o:.1f}s "
        f"total={time.time()-t0:.1f}s syms={out_df.height}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--days", type=dt.date.fromisoformat, nargs="*")
    ap.add_argument("--stride", type=int, default=1, help="每隔 k 天取一天（IC 筛选不需要连续日）")
    ap.add_argument("--reverse", action="store_true")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    ledger = parse_ledger(LEDGER.read_text(encoding="utf-8"))
    days = tuple(a.days) if a.days else RawStore(ROOT, ledger).days()
    if a.reverse:
        days = tuple(reversed(days))
    if a.stride > 1:
        days = days[::a.stride]
    jobs = [(d, a.out) for d in days]
    if a.workers <= 1:
        for j in jobs:
            print(build_day(j), flush=True)
    else:
        with mp.get_context("spawn").Pool(a.workers) as pool:
            for msg in pool.imap_unordered(build_day, jobs):
                print(msg, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())