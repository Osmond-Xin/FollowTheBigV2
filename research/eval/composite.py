"""合成信号：给定 (因子, 符号) 列表，日内秩平均成一个分数，再跑 IC / 分组 / 换手 / 年度表。

定义写死在 SPECS 里；2026-09-04 在 2025–2026（发现集）上定，2022–2024（确认集）在跑之前不得改。
用法：PYTHONPATH=src uv run python research/eval/composite.py --panels DIR... --spec algo_footprint [--start --end]
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import polars as pl

from factor_eval import add_returns, ic_summary, load_panel, neutralize, quantile_returns, rank_ic, turnover, universe

# 「算法拆单执行中」足迹：单手委托多、方向持续、被撤买单命短、中等规模(2–5万)成交少、收盘竞价占比低
SPECS: dict[str, tuple[tuple[str, int], ...]] = {
    "algo_footprint": (("qf_lot1_b", +1), ("f_lot1", +1), ("t_sign_ac1", +1), ("q_life_med_b", -1),
                       ("r_share_b2", -1), ("t_amt_share_close_call", -1)),
    "algo_footprint_3": (("qf_lot1_b", +1), ("t_sign_ac1", +1), ("q_life_med_b", -1)),
    "lot1_only": (("qf_lot1_b", +1),),
    # 2026-09-04 看过 2024 之后定的精简版：只留 2024 也同号显著的成分；2022–2023 对它仍是干净的确认集
    "core": (("qf_lot1_b", +1), ("t_sign_ac1", +1), ("pre_cx_b", -1), ("rkurt", -1)),
}


def composite(panel: pl.DataFrame, spec: tuple[tuple[str, int], ...], name: str, neu: bool) -> pl.DataFrame:
    cols = []
    for f, sgn in spec:
        if neu:
            panel = neutralize(panel, f); f = f + "_neu"
        cols.append(sgn * (pl.col(f).rank().over("day") / pl.col(f).count().over("day")))
    return panel.with_columns(pl.mean_horizontal(cols).alias(name))


def report(panel: pl.DataFrame, name: str, horizons=("fwd_5", "fwd_10", "fwd_20", "fwdo_10")) -> pl.DataFrame:
    rows = []
    for h in horizons:
        n = int(h.rsplit("_", 1)[1])
        ic = ic_summary(rank_ic(panel, name, h), lag=n)
        q = quantile_returns(panel, name, h)
        t = turnover(panel, name)["avg_daily_turnover"]
        rows.append({"horizon": h, "mean_ic": ic["mean"], "icir": ic["icir"], "t_nw": ic["t_nw"], "pct_pos": ic["pct_pos"],
                     "n_days": ic["n_days"], "spread_ann": q["spread_ann"], "sharpe": q["sharpe"],
                     "top_gross": q["decile_ann_ret"].get(10), "top_net_holdN": q["decile_ann_ret"].get(10) - 244 / n * 0.0015,
                     "top_excess_ann": q["top_excess_ann"], "monotonic": q["monotonic"], "daily_turnover": t,
                     "ic_by_year": str({k: round(v, 4) for k, v in ic["by_year"].items()}), "spread_by_year": str({k: round(v, 3) for k, v in q["year_spread"].items()})})
    return pl.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panels", nargs="+", type=Path, required=True)
    ap.add_argument("--spec", default="algo_footprint")
    ap.add_argument("--start", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--end", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--raw", action="store_true", help="不中性化")
    a = ap.parse_args()
    p = universe(add_returns(load_panel(a.panels)))
    if a.start: p = p.filter(pl.col("day") >= a.start)
    if a.end: p = p.filter(pl.col("day") <= a.end)
    p = composite(p, SPECS[a.spec], "score", neu=not a.raw)
    pl.Config.set_tbl_cols(20); pl.Config.set_tbl_width_chars(220); pl.Config.set_float_precision(3); pl.Config.set_fmt_str_lengths(120)
    print(report(p, "score"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
