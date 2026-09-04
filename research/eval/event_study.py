"""事件研究：给一个布尔条件（polars 表达式字符串），报事件日之后 N 日收益分布，与同日宇宙对照，双向都报。

用法：PYTHONPATH=src uv run python research/eval/event_study.py --panels DIR... --cond "(pl.col('p_net_b5') > 0.05) & (pl.col('ret1').abs() < 0.01)" [--start --end]
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import numpy as np
import polars as pl
import statsmodels.api as sm

from factor_eval import add_returns, load_panel, universe

HORIZONS = ("fwdo_1", "fwdo_5", "fwdo_10", "fwdo_20", "fwd_1", "fwd_5", "fwd_10", "fwd_20")


def event_table(panel: pl.DataFrame, cond: pl.Expr) -> pl.DataFrame:
    """每个持有期一行：事件数、事件日均收益、宇宙同日均收益、超额、NW-t、胜率(超额>0 的事件占比)、超额分位。"""
    base = panel.filter(pl.col("in_univ_exec"))
    ev = base.filter(cond)
    rows = []
    for h in HORIZONS:
        n = int(h.rsplit("_", 1)[1])
        um = base.group_by("day").agg(pl.col(h).mean().alias("u"))
        e = ev.join(um, on="day").filter(pl.col(h).is_not_null() & pl.col("u").is_not_null())
        if e.height < 5:
            rows.append({"horizon": h, "n_events": e.height}); continue
        ex = (e[h] - e["u"]).to_numpy()
        # 按日聚合再做 NW-t，避免同日事件的截面相关被当成独立样本
        d = e.group_by("day").agg((pl.col(h) - pl.col("u")).mean().alias("x")).sort("day")
        x = d["x"].to_numpy()
        t = float(sm.OLS(x, np.ones((len(x), 1))).fit(cov_type="HAC", cov_kwds={"maxlags": max(n, 1)}).tvalues[0]) if len(x) > 2 else float("nan")
        rows.append({"horizon": h, "n_events": e.height, "n_days": d.height,
                     "ev_mean": float(e[h].mean()), "univ_mean": float(e["u"].mean()),
                     "excess_mean": float(ex.mean()), "excess_median": float(np.median(ex)),
                     "t_nw_daily": t, "hit_rate": float((ex > 0).mean()),
                     "p10": float(np.quantile(ex, 0.1)), "p90": float(np.quantile(ex, 0.9)),
                     "events_per_day": e.height / max(d.height, 1)})
    return pl.DataFrame(rows)


def by_year(panel: pl.DataFrame, cond: pl.Expr, h: str) -> pl.DataFrame:
    base = panel.filter(pl.col("in_univ_exec"))
    um = base.group_by("day").agg(pl.col(h).mean().alias("u"))
    e = base.filter(cond).join(um, on="day").filter(pl.col(h).is_not_null())
    return e.with_columns(year=pl.col("day").dt.year(), ex=pl.col(h) - pl.col("u")).group_by("year").agg(
        n=pl.len(), excess_mean=pl.col("ex").mean(), hit=(pl.col("ex") > 0).mean()).sort("year")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panels", nargs="+", type=Path, required=True)
    ap.add_argument("--cond", required=True, help="polars 表达式，可用 pl")
    ap.add_argument("--start", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--end", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--year-horizon", default="fwdo_10")
    a = ap.parse_args()
    p = universe(add_returns(load_panel(a.panels)))
    if a.start: p = p.filter(pl.col("day") >= a.start)
    if a.end: p = p.filter(pl.col("day") <= a.end)
    cond = eval(a.cond, {"pl": pl})
    pl.Config.set_tbl_cols(20); pl.Config.set_tbl_width_chars(200); pl.Config.set_float_precision(4)
    print(event_table(p, cond))
    print(by_year(p, cond, a.year_horizon))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
