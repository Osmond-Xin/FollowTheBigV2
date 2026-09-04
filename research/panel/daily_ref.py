"""日级参考层：从 xinqing（3 秒十档快照）一趟扫出每个 (标的, 日) 的 OHLC / 前收 / 量额 / 盘口特征。

用法：PYTHONPATH=src uv run python research/panel/daily_ref.py --out /Volumes/xin/FollowTheBigV2-derived/daily_ref [--workers 4] [--days ...]
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
from ftbv2.core.raw.schema import AM_START_MS, PM_END_MS, AM_END_MS, PM_START_MS
from ftbv2.io.raw import RawStore

ROOT = Path("/Volumes/辛的硬盘/data/preserve")
LEDGER = Path("ledger/defects.toml")
FIELDS = ("time_ms", "last_price", "cum_vol", "cum_amt", "n_trades", "high", "low", "open", "prev_close",
          "ask_px_1", "bid_px_1", "ask_total", "bid_total",
          *[f"ask_sz_{k}" for k in range(1, 11)], *[f"bid_sz_{k}" for k in range(1, 11)])
CLOSE_CALL_MS = PM_END_MS                 # 14:57
LAST15_MS = (14 * 3600 + 45 * 60) * 1000  # 14:45
FIRST15_MS = (9 * 3600 + 45 * 60) * 1000  # 09:45


def features(x: pl.DataFrame) -> pl.DataFrame:
    x = x.sort("symbol", "time_ms")
    ask10 = sum(pl.col(f"ask_sz_{k}") for k in range(1, 11))
    bid10 = sum(pl.col(f"bid_sz_{k}") for k in range(1, 11))
    mid = (pl.col("ask_px_1") + pl.col("bid_px_1")) / 2
    valid_book = (pl.col("ask_px_1") > 0) & (pl.col("bid_px_1") > 0)
    x = x.with_columns(
        imb1=((pl.col("bid_sz_1") - pl.col("ask_sz_1")) / (pl.col("bid_sz_1") + pl.col("ask_sz_1"))),
        imb10=((bid10 - ask10) / (bid10 + ask10)),
        imb_total=((pl.col("bid_total") - pl.col("ask_total")) / (pl.col("bid_total") + pl.col("ask_total"))),
        spread_bp=pl.when(valid_book).then((pl.col("ask_px_1") - pl.col("bid_px_1")) / mid * 1e4).otherwise(None),
        depth10=(bid10 + ask10),
        cont=((pl.col("time_ms") >= AM_START_MS) & (pl.col("time_ms") < AM_END_MS))
        | ((pl.col("time_ms") >= PM_START_MS) & (pl.col("time_ms") < CLOSE_CALL_MS)),
        last15=(pl.col("time_ms") >= LAST15_MS) & (pl.col("time_ms") < CLOSE_CALL_MS),
        first15=(pl.col("time_ms") >= AM_START_MS) & (pl.col("time_ms") < FIRST15_MS),
        closecall=(pl.col("time_ms") >= CLOSE_CALL_MS),
        no_ask=(pl.col("ask_px_1") == 0) & (pl.col("bid_px_1") > 0),   # 涨停封板形态：无卖一
        no_bid=(pl.col("bid_px_1") == 0) & (pl.col("ask_px_1") > 0),   # 跌停封板形态：无买一
    )
    def mean_in(col: str, mask: str) -> pl.Expr:
        return pl.col(col).filter(pl.col(mask)).mean()
    last = pl.all().sort_by("time_ms").last()
    g = x.group_by("day", "symbol").agg(
        n_snap=pl.len(),
        close=pl.col("last_price").last(),
        open=pl.col("open").last(), high=pl.col("high").last(), low=pl.col("low").last(),
        prev_close=pl.col("prev_close").last(),
        vol=pl.col("cum_vol").last(), amt=pl.col("cum_amt").last(), n_trades=pl.col("n_trades").last(),
        close_ask1=pl.col("ask_px_1").last(), close_bid1=pl.col("bid_px_1").last(),
        close_ask_total=pl.col("ask_total").last(), close_bid_total=pl.col("bid_total").last(),
        # 收盘集合竞价前最后一帧（14:57 前）的簿
        pre_call_bid_total=pl.col("bid_total").filter(pl.col("cont")).last(),
        pre_call_ask_total=pl.col("ask_total").filter(pl.col("cont")).last(),
        imb1_mean=mean_in("imb1", "cont"), imb10_mean=mean_in("imb10", "cont"), imb_total_mean=mean_in("imb_total", "cont"),
        imb10_last15=mean_in("imb10", "last15"), imb_total_last15=mean_in("imb_total", "last15"),
        imb10_first15=mean_in("imb10", "first15"), imb_total_first15=mean_in("imb_total", "first15"),
        imb10_closecall=mean_in("imb10", "closecall"), imb_total_closecall=mean_in("imb_total", "closecall"),
        spread_bp_mean=mean_in("spread_bp", "cont"),
        depth10_mean=mean_in("depth10", "cont"),
        bid_total_mean=mean_in("bid_total", "cont"), ask_total_mean=mean_in("ask_total", "cont"),
        frac_no_ask=pl.col("no_ask").filter(pl.col("cont")).mean(),
        frac_no_bid=pl.col("no_bid").filter(pl.col("cont")).mean(),
        # 尾盘 15 分钟成交额占比（当日成交额 - 14:45 时累计）
        amt_at_1445=pl.col("cum_amt").filter(pl.col("time_ms") < LAST15_MS).last(),
        amt_at_0945=pl.col("cum_amt").filter(pl.col("time_ms") < FIRST15_MS).last(),
        amt_at_1457=pl.col("cum_amt").filter(pl.col("time_ms") < CLOSE_CALL_MS).last(),
        amt_at_0930=pl.col("cum_amt").filter(pl.col("time_ms") < AM_START_MS).last(),
    )
    return g.sort("symbol")


def build_day(args: tuple[dt.date, Path]) -> str:
    day, out = args
    path = out / f"date={day:%Y%m%d}.parquet"
    if path.exists():
        return f"{day} skip"
    t0 = time.time()
    ledger = parse_ledger(LEDGER.read_text(encoding="utf-8"))
    st = RawStore(ROOT, ledger)
    req = ReadRequest("xinqing", (day,), FIELDS)
    res = st.execute(plan(req, st.catalog("xinqing", (day,)), ledger))
    if res.frame.height == 0:
        return f"{day} EMPTY gaps={res.gaps}"
    g = features(res.frame)
    tmp = path.with_suffix(".tmp")
    g.write_parquet(tmp)
    tmp.rename(path)
    return f"{day} rows={res.frame.height} syms={g.height} {time.time()-t0:.1f}s"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--days", type=dt.date.fromisoformat, nargs="*")
    ap.add_argument("--reverse", action="store_true", help="从最近的一天往回跑")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    ledger = parse_ledger(LEDGER.read_text(encoding="utf-8"))
    days = tuple(a.days) if a.days else RawStore(ROOT, ledger).days()
    if a.reverse:
        days = tuple(reversed(days))
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
