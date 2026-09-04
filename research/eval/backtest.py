"""组合回测：每个信号日按合成分数选前 top_frac，次日开盘等权买入，持 hold 个交易日后收盘卖出；
各批次重叠时资金等分（每批占 1/批数）。扣费：往返 0.15%。同时报同样规则的宇宙等权基准、多空（多高档空低档）。
输出：年度收益、总年化、波动、Sharpe、最大回撤、平均持仓数。信号日是 flow 面板存在的日子（取样步长 3 时每 3 天一批）。

用法：PYTHONPATH=src:research/eval uv run python research/eval/backtest.py --panels DIR... --spec core [--top 0.1] [--hold 10] [--start --end]
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


def tranche_returns(panel: pl.DataFrame, score: str, top: float, hold: int) -> pl.DataFrame:
    """每个信号日一行：高档批次收益、低档批次收益、宇宙批次收益（都是 open_{t+1} → close_{t+hold}，已扣费）。"""
    h = f"fwdo_{hold}"
    d = panel.filter(pl.col("in_univ_exec") & pl.col(score).is_not_null() & pl.col(h).is_not_null())
    d = d.with_columns(pct=pl.col(score).rank().over("day") / pl.col(score).count().over("day"))
    return d.group_by("day").agg(
        top=pl.col(h).filter(pl.col("pct") > 1 - top).mean() - COST,
        bot=pl.col(h).filter(pl.col("pct") <= top).mean() - COST,
        univ=pl.col(h).mean() - COST,
        n_top=(pl.col("pct") > 1 - top).sum(),
    ).sort("day")


def equity(tr: pl.DataFrame, col: str, hold: int, calendar: list[dt.date]) -> pl.DataFrame:
    """把批次收益摊到日：批次在 [t+1, t+hold] 持有，每日收益 = 该批次收益/hold（线性摊，足够看年度与回撤）。"""
    idx = {d: i for i, d in enumerate(calendar)}
    daily = np.zeros(len(calendar)); active = np.zeros(len(calendar))
    for day, r in zip(tr["day"].to_list(), tr[col].to_list()):
        if r is None or day not in idx: continue
        i = idx[day]
        for k in range(1, hold + 1):
            if i + k < len(calendar):
                daily[i + k] += r / hold; active[i + k] += 1
    ret = np.where(active > 0, daily / np.maximum(active, 1), 0.0)   # 批次间资金等分
    return pl.DataFrame({"day": calendar, "ret": ret, "n_active": active})


def stats(eq: pl.DataFrame) -> dict:
    r = eq["ret"].to_numpy(); n = len(r)
    cum = np.cumprod(1 + r); dd = cum / np.maximum.accumulate(cum) - 1
    yrs = eq.with_columns(year=pl.col("day").dt.year()).group_by("year").agg(((1 + pl.col("ret")).product() - 1).alias("r")).sort("year")
    return {"ann_ret": float(cum[-1] ** (ANN / n) - 1), "ann_vol": float(r.std() * np.sqrt(ANN)),
            "sharpe": float(r.mean() / r.std() * np.sqrt(ANN)) if r.std() > 0 else float("nan"),
            "max_dd": float(dd.min()), "by_year": {int(y): round(v, 4) for y, v in zip(yrs["year"], yrs["r"])}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panels", nargs="+", type=Path, required=True)
    ap.add_argument("--spec", default="core"); ap.add_argument("--top", type=float, default=0.1)
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--start", type=dt.date.fromisoformat, default=None); ap.add_argument("--end", type=dt.date.fromisoformat, default=None)
    a = ap.parse_args()
    p = universe(add_returns(load_panel(a.panels)))
    if a.start: p = p.filter(pl.col("day") >= a.start)
    if a.end: p = p.filter(pl.col("day") <= a.end)
    p = composite(p, SPECS[a.spec], "score", neu=True)
    tr = tranche_returns(p, "score", a.top, a.hold)
    cal = sorted(p["day"].unique().to_list())
    print(f"spec={a.spec} top={a.top} hold={a.hold} 信号日={tr.height} 平均持仓={tr['n_top'].mean():.0f}")
    for col in ("top", "univ", "bot"):
        s = stats(equity(tr, col, a.hold, cal)); print(f"{col:5s} 年化 {s['ann_ret']:+.3f} 波动 {s['ann_vol']:.3f} Sharpe {s['sharpe']:.2f} 最大回撤 {s['max_dd']:.3f} 分年 {s['by_year']}")
    ls = tr.with_columns(ls=pl.col("top") - pl.col("bot"))
    s = stats(equity(ls, "ls", a.hold, cal)); print(f"多空   年化 {s['ann_ret']:+.3f} 波动 {s['ann_vol']:.3f} Sharpe {s['sharpe']:.2f} 最大回撤 {s['max_dd']:.3f} 分年 {s['by_year']}")
    ex = tr.with_columns(ex=pl.col("top") - pl.col("univ"))
    s = stats(equity(ex, "ex", a.hold, cal)); print(f"多头超额 年化 {s['ann_ret']:+.3f} Sharpe {s['sharpe']:.2f} 最大回撤 {s['max_dd']:.3f} 分年 {s['by_year']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
