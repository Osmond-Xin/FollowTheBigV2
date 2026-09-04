"""逐日盯市回测：每个信号日 t 选前 top 比例，t+1 开盘等权买入，持 hold 个交易日到收盘卖出；
每个批次每天按持仓名字的真实日收益盯市（第 1 天 close/open−1，之后 close/prev_close−1；停牌日记 0），
组合日收益 = 活跃批次等权平均；往返成本 0.15% 在买入日一次扣除。报多头 / 宇宙基准 / 低档 / 多空 / 多头超额。

用法：PYTHONPATH=src:research/eval uv run python research/eval/backtest_mtm.py --panels DIR... --spec core --top 0.1 --hold 10
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import numpy as np
import polars as pl

from composite import SPECS, composite
from factor_eval import add_returns, load_panel, universe

COST = 0.0015
ANN = 244


def holdings(panel: pl.DataFrame, score: str, top: float, which: str) -> pl.DataFrame:
    d = panel.filter(pl.col("in_univ_exec") & pl.col(score).is_not_null())
    d = d.with_columns(pct=pl.col(score).rank().over("day") / pl.col(score).count().over("day"))
    if which == "top":
        d = d.filter(pl.col("pct") > 1 - top)
    elif which == "bot":
        d = d.filter(pl.col("pct") <= top)
    return d.select(entry=pl.col("day"), symbol=pl.col("symbol"))


def daily_series(hold_tbl: pl.DataFrame, panel: pl.DataFrame, calendar: list[dt.date], hold: int) -> pl.DataFrame:
    idx = pl.DataFrame({"entry": calendar, "i": list(range(len(calendar)))})
    cal = pl.DataFrame({"day": calendar, "j": list(range(len(calendar)))})
    h = hold_tbl.join(idx, on="entry").with_columns(k=pl.lit(list(range(1, hold + 1)))).explode("k")
    h = h.with_columns(j=pl.col("i") + pl.col("k")).join(cal, on="j", how="inner")
    px = panel.select("day", "symbol", "open", "close", "prev_close").with_columns(
        r_first=pl.when((pl.col("open") > 0) & (pl.col("close") > 0)).then(pl.col("close") / pl.col("open") - 1).otherwise(0.0),
        r_next=pl.when((pl.col("prev_close") > 0) & (pl.col("close") > 0)).then(pl.col("close") / pl.col("prev_close") - 1).otherwise(0.0),
    )
    h = h.join(px, on=["day", "symbol"], how="left").with_columns(
        r=pl.when(pl.col("k") == 1).then(pl.col("r_first") - COST).otherwise(pl.col("r_next")).fill_null(0.0)
    )
    tranche = h.group_by("entry", "day").agg(pl.col("r").mean().alias("tr"))
    return tranche.group_by("day").agg(pl.col("tr").mean().alias("ret"), pl.len().alias("n_active")).sort("day")


def stats(s: pl.DataFrame) -> str:
    r = s["ret"].to_numpy(); n = len(r)
    cum = np.cumprod(1 + r); dd = cum / np.maximum.accumulate(cum) - 1
    yrs = s.with_columns(year=pl.col("day").dt.year()).group_by("year").agg(((1 + pl.col("ret")).product() - 1).alias("r")).sort("year")
    by = {int(y): round(v, 3) for y, v in zip(yrs["year"], yrs["r"])}
    sh = r.mean() / r.std() * np.sqrt(ANN) if r.std() > 0 else float("nan")
    return f"年化 {cum[-1] ** (ANN / n) - 1:+.3f} 波动 {r.std() * np.sqrt(ANN):.3f} Sharpe {sh:.2f} 最大回撤 {dd.min():.3f} 分年 {by}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panels", nargs="+", type=Path, required=True)
    ap.add_argument("--spec", default="core"); ap.add_argument("--top", type=float, default=0.1); ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--start", type=dt.date.fromisoformat, default=None); ap.add_argument("--end", type=dt.date.fromisoformat, default=None)
    a = ap.parse_args()
    p = universe(add_returns(load_panel(a.panels)))
    if a.start: p = p.filter(pl.col("day") >= a.start)
    if a.end: p = p.filter(pl.col("day") <= a.end)
    p = composite(p, SPECS[a.spec], "score", neu=True)
    cal = sorted(p["day"].unique().to_list())
    out = {}
    for w in ("top", "univ", "bot"):
        ht = holdings(p, "score", a.top, w)
        out[w] = daily_series(ht, p, cal, a.hold)
        print(f"{w:5s} {stats(out[w])}  信号日 {ht['entry'].n_unique()} 平均持仓 {ht.height / max(ht['entry'].n_unique(), 1):.0f}")
    j = out["top"].join(out["bot"], on="day", suffix="_b").with_columns(ret=pl.col("ret") - pl.col("ret_b"))
    print("多空  ", stats(j.select("day", "ret")))
    j = out["top"].join(out["univ"], on="day", suffix="_u").with_columns(ret=pl.col("ret") - pl.col("ret_u"))
    print("多头超额", stats(j.select("day", "ret")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
