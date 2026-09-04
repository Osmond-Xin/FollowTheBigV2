"""横截面因子评价库：IC / 分层收益 / 换手 / 中性化，一站式跑一遍因子体检。

面板列见 research/panel/daily_ref.py。close/open/prev_close 中的 0 视为"当天没有成交"
（全天停牌），一律当缺失处理：不产生收益、不进入 universe、远期收益链条在缺失的那天
"跳过"而不是把它算成 -100%（依赖 prev_close 是交易所口径的复权前收，跳过的那天的收益
会被下一交易日的 ret1 完整吸收，见 add_returns 的实现说明）。

用法：
    PYTHONPATH=src uv run python research/eval/factor_eval.py \
        --panels /Volumes/xin/FollowTheBigV2-derived/daily_ref \
        --factors imb10_mean,spread_bp_mean \
        --out research/eval/out/daily_ref_v0
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import polars as pl
import statsmodels.api as sm
from scipy import stats

HORIZONS = (1, 2, 5, 10, 20)
COST_PER_ROUND_TRIP = 0.0015  # 佣金 0.025%×2 + 印花税 0.05% + 滑点 0.05%
ANN_DAYS = 244

# 面板原始列 + 我们自己派生的列，都不作为"默认因子"被自动发现（要测就用 --factors 显式列出）
NON_FACTOR_COLS = {
    "day", "symbol", "day_idx",
    "close", "open", "high", "low", "prev_close", "vol", "amt", "n_trades",
    "close_ask1", "close_bid1", "close_ask_total", "close_bid_total",
    "pre_call_bid_total", "pre_call_ask_total",
    "amt_at_1445", "amt_at_0945", "amt_at_1457", "amt_at_0930",
    "n_snap", "frac_no_ask", "frac_no_bid",
    "ret1", "mom20", "amt20", "log_amt20", "age",
    "amt_share_last15", "amt_share_first15", "amt_share_closecall",
    "in_univ", "in_univ_exec",
    *(f"fwd_{n}" for n in HORIZONS), *(f"fwdo_{n}" for n in HORIZONS),
}


def load_panel(dirs: list[Path]) -> pl.DataFrame:
    """扫描每个目录下的所有 parquet，按 (day, symbol) 依次左连接（第一个目录为主表）。"""
    lfs = [pl.scan_parquet(sorted(Path(d).glob("*.parquet"))) for d in dirs]
    panel = lfs[0]
    for lf in lfs[1:]:
        panel = panel.join(lf, on=("day", "symbol"), how="left")
    return panel.sort("symbol", "day").collect()


def _full_grid(panel: pl.DataFrame) -> pl.DataFrame:
    """symbol × 交易日历 的满网格，把该 symbol 当天完全没有数据的行也占位出来（值为 null）。"""
    calendar = panel.select("day").unique().sort("day")
    symbols = panel.select("symbol").unique().sort("symbol")
    grid = symbols.join(calendar, how="cross")
    return grid.join(panel, on=("day", "symbol"), how="left").sort("symbol", "day")


def add_returns(panel: pl.DataFrame) -> pl.DataFrame:
    """补齐 symbol×日历 满网格，加当日收益/远期收益/滚动特征。

    远期收益直接 close_{t+N}/close_t 相除——窗口中间即使有整天停牌也不影响：
    prev_close 是复权前收，停牌日 ret1=null，下一交易日 ret1(= close/最近一次收盘)
    已把跨停牌的累计涨跌吸收进了 close/prev_close 比值，两端 close 都非空时直接
    相除等价于跨越缺口也成立的复权连乘（不能用 cumlog.fill_null(0)：那种写法
    会丢掉停牌日被 ret1[k+1] 吸收掉的那段收益，见 test_forward_return_matches_
    direct_computation_across_missing_day 的反例）。
    """
    g = _full_grid(panel)
    # close/open/prev_close 里的 0 表示当天完全没有成交（全天停牌），一律当缺失处理。
    g = g.with_columns(
        close=pl.when(pl.col("close") > 0).then(pl.col("close")).otherwise(None),
        open=pl.when(pl.col("open") > 0).then(pl.col("open")).otherwise(None),
        prev_close=pl.when(pl.col("prev_close") > 0).then(pl.col("prev_close")).otherwise(None),
    )
    g = g.with_columns(
        day_idx=pl.col("day").rank(method="dense").cast(pl.Int32) - 1,
        ret1=(pl.col("close") / pl.col("prev_close") - 1),
    )

    # 远期收益直接 close_{t+N}/close_t 相除——窗口中间即使有整天停牌也不影响。
    # 理由是 prev_close 是复权前收，停牌日 ret1=null、下个交易日的 ret1(= close/最近一次收盘)
    # 已经把跨停牌的累计涨跌吸收进了 close/prev_close 比值；两端 close 都非空时直接相除
    # 等价于"跨越缺口也成立"的复权连乘。把这个实现走成 cumlog.fill_null(0) 会丢掉停牌日
    # 那段被 ret1[k+1] 吸收掉的收益（test_forward_return_matches_direct_computation_across_missing_day
    # 验证），所以这一支必须用直接除法而不是对数日收益的累加。
    # 复权连乘：ret1 = close/prev_close − 1 已含除权除息（prev_close 是交易所复权前收）。
    # 全网格上停牌日 close 为 null ⇒ ret1 null ⇒ 记 0；复牌日 ret1 = close/停牌前收，把缺口吸收进去。
    # 直接 close_{t+N}/close_t 相除会在窗口内有除权除息时给出假的大幅负收益，所以不用。
    g = g.with_columns(lr=(pl.col("ret1") + 1).log().fill_null(0.0).fill_nan(0.0))
    g = g.with_columns(cumlr=pl.col("lr").cum_sum().over("symbol"))
    exprs = []
    for n in HORIZONS:
        close_fwd = pl.col("close").shift(-n).over("symbol")
        cum_fwd = pl.col("cumlr").shift(-n).over("symbol")
        valid = pl.col("close").is_not_null() & close_fwd.is_not_null()
        fwd = (cum_fwd - pl.col("cumlr")).exp() - 1
        exprs.append(pl.when(valid).then(fwd).otherwise(None).alias(f"fwd_{n}"))
        # fwdo_N = 复权 close_{t+N} / open_{t+1} − 1 = (1+fwd_N) × prev_close_{t+1} / open_{t+1} − 1
        # （prev_close_{t+1} 是复权后的 close_t，隔夜段用它与 open_{t+1} 相比才对）。
        open_next1 = pl.col("open").shift(-1).over("symbol")
        pc_next1 = pl.col("prev_close").shift(-1).over("symbol")
        valid_o = valid & open_next1.is_not_null() & pc_next1.is_not_null() & (open_next1 > 0)
        exprs.append(pl.when(valid_o).then((1 + fwd) * pc_next1 / open_next1 - 1).otherwise(None).alias(f"fwdo_{n}"))
    g = g.with_columns(exprs).drop("lr")

    close_20ago = pl.col("close").shift(20).over("symbol")
    cum_20ago = pl.col("cumlr").shift(20).over("symbol")
    g = g.with_columns(
        mom20=pl.when(pl.col("close").is_not_null() & close_20ago.is_not_null())
        .then((pl.col("cumlr") - cum_20ago).exp() - 1)
        .otherwise(None),
        amt20=pl.col("amt").rolling_mean(window_size=20, min_samples=1).over("symbol"),
    )
    g = g.with_columns(
        log_amt20=pl.when(pl.col("amt20") > 0).then(pl.col("amt20").log()).otherwise(None),
        amt_share_last15=pl.when(pl.col("amt") > 0)
        .then((pl.col("amt") - pl.col("amt_at_1445")) / pl.col("amt"))
        .otherwise(None),
        amt_share_first15=pl.when(pl.col("amt") > 0)
        .then((pl.col("amt_at_0945") - pl.col("amt_at_0930")) / pl.col("amt"))
        .otherwise(None),
        amt_share_closecall=pl.when(pl.col("amt") > 0)
        .then((pl.col("amt") - pl.col("amt_at_1457")) / pl.col("amt"))
        .otherwise(None),
    )

    first_idx = (
        g.filter(pl.col("close").is_not_null())
        .group_by("symbol")
        .agg(pl.col("day_idx").min().alias("first_idx"))
    )
    g = g.join(first_idx, on="symbol", how="left").with_columns(age=pl.col("day_idx") - pl.col("first_idx"))
    return g.drop("first_idx", "cumlr").sort("symbol", "day")


def universe(panel: pl.DataFrame, min_amt: float = 1e7, min_age: int = 60) -> pl.DataFrame:
    """加 in_univ（可用于 fwd_N）和 in_univ_exec（额外要求次日开盘可执行，用于 fwdo_N）。"""
    open_next = pl.col("open").shift(-1).over("symbol")
    base = (
        (pl.col("amt20") >= min_amt)
        & (pl.col("age") >= min_age)
        & (pl.col("n_snap") >= 1000)
        & (pl.col("frac_no_ask") < 0.5)
        & (pl.col("frac_no_bid") < 0.5)
        & (pl.col("ret1").abs() < 0.095)
    )
    exec_ok = open_next.is_not_null() & ((open_next / pl.col("close") - 1).abs() < 0.095)
    return panel.with_columns(
        in_univ=base.fill_null(False),
        in_univ_exec=(base & exec_ok).fill_null(False),
    )


def _univ_col(horizon_col: str) -> str:
    return "in_univ_exec" if horizon_col.startswith("fwdo_") else "in_univ"


def _horizon_n(horizon_col: str) -> int:
    return int(horizon_col.rsplit("_", 1)[-1])


def rank_ic(panel: pl.DataFrame, factor: str, horizon_col: str) -> pl.DataFrame:
    """每日 universe 内 factor 与 forward return 的 Spearman 秩相关（不翻符号）。"""
    d = panel.filter(
        pl.col(_univ_col(horizon_col)) & pl.col(factor).is_not_null() & pl.col(horizon_col).is_not_null()
    )
    d = d.with_columns(fr=pl.col(factor).rank().over("day"), hr=pl.col(horizon_col).rank().over("day"))
    return d.group_by("day").agg(pl.corr("fr", "hr").alias("ic"), n=pl.len()).sort("day")


def ic_summary(ic_df: pl.DataFrame, lag: int) -> dict:
    # 既有 null 又有 NaN：null 是 group_by 没产出（不应该有），NaN 是某日 corr 退化。
    # NaN 会污染 mean/std/OLS，所以先把 NaN 当成 drop。
    d = ic_df.filter(pl.col("ic").is_not_null() & pl.col("ic").is_finite())
    ic = d["ic"].to_numpy()
    n = ic.shape[0]
    if n == 0:
        return {"mean": float("nan"), "std": float("nan"), "icir": float("nan"), "t_nw": float("nan"),
                "pct_pos": float("nan"), "n_days": 0, "by_year": {}}
    mean = float(ic.mean())
    std = float(ic.std(ddof=1)) if n > 1 else float("nan")
    model = sm.OLS(ic, np.ones((n, 1))).fit(cov_type="HAC", cov_kwds={"maxlags": max(lag, 1)})
    by_year = d.with_columns(year=pl.col("day").dt.year()).group_by("year").agg(
        pl.col("ic").mean().alias("m")
    ).sort("year")
    return {
        "mean": mean, "std": std, "icir": (mean / std) if std else float("nan"),
        "t_nw": float(model.tvalues[0]), "pct_pos": float((ic > 0).mean()), "n_days": n,
        "by_year": dict(zip(by_year["year"].to_list(), by_year["m"].to_list())),
    }


def _add_decile(d: pl.DataFrame, factor: str, n: int) -> pl.DataFrame:
    # polars 的 rank(NaN) 会给 NaN 一个最大排名（不是 null），会让一列几乎全是 NaN 的因子
    # 全挤进最高 decile，把分层收益算空。先把 NaN fill 成 null，分母只用非 null 计数，
    # null 分位的行 decile 也是 null，下游 quantile_returns 不看它们。
    x = pl.col(factor).fill_nan(None)
    rank = x.rank(method="average").over("day")
    valid_per_day = x.is_not_null().sum().over("day")
    pct = rank / valid_per_day
    return d.with_columns(decile=(pct * n).ceil().clip(1, n).cast(pl.Int32))


def quantile_returns(panel: pl.DataFrame, factor: str, horizon_col: str, n: int = 10) -> dict:
    d = panel.filter(
        pl.col(_univ_col(horizon_col)) & pl.col(factor).is_not_null() & pl.col(horizon_col).is_not_null()
    )
    d = _add_decile(d, factor, n)
    d = d.filter(pl.col("decile").is_not_null())
    N = _horizon_n(horizon_col)
    ann = ANN_DAYS / N

    daily = d.group_by("day", "decile").agg(pl.col(horizon_col).mean().alias("ret"))
    dec_mean = daily.group_by("decile").agg(pl.col("ret").mean().alias("m")).sort("decile")
    dec_mean = dec_mean.with_columns(ann_ret=pl.col("m") * ann)

    wide = daily.pivot(values="ret", index="day", on="decile").sort("day")
    top_c, bot_c = str(n), "1"
    if top_c in wide.columns and bot_c in wide.columns:
        spread_df = wide.select("day", (pl.col(top_c) - pl.col(bot_c)).alias("spread")).drop_nulls("spread")
    else:
        spread_df = wide.select("day").with_columns(spread=pl.lit(None, dtype=pl.Float64)).drop_nulls("spread")
    spread = spread_df["spread"].to_numpy()

    t_spread = float("nan")
    if spread.shape[0] > 1:
        model = sm.OLS(spread, np.ones((spread.shape[0], 1))).fit(
            cov_type="HAC", cov_kwds={"maxlags": max(N, 1)}
        )
        t_spread = float(model.tvalues[0])
    spread_ann = float(spread.mean() * ann) if spread.shape[0] else float("nan")
    sharpe = (
        float(spread.mean() / spread.std(ddof=1) * np.sqrt(ann))
        if spread.shape[0] > 1 and spread.std(ddof=1) > 0
        else float("nan")
    )

    univ_daily_mean = d.group_by("day").agg(pl.col(horizon_col).mean().alias("m"))["m"]
    univ_mean = float(univ_daily_mean.mean()) if univ_daily_mean.len() else float("nan")
    top_row = dec_mean.filter(pl.col("decile") == n)
    top_mean = float(top_row["m"].item()) if top_row.height else float("nan")
    top_excess_ann = (top_mean - univ_mean) * ann if univ_daily_mean.len() else float("nan")

    mono = float("nan")
    if dec_mean.height > 2:
        rho, _ = stats.spearmanr(dec_mean["decile"].to_numpy(), dec_mean["m"].to_numpy())
        mono = float(rho)

    year_spread = {}
    if top_c in wide.columns and bot_c in wide.columns:
        ys = (
            wide.with_columns(year=pl.col("day").dt.year(), spread=pl.col(top_c) - pl.col(bot_c))
            .drop_nulls("spread")
            .group_by("year")
            .agg(pl.col("spread").mean().alias("s"))
            .sort("year")
        )
        year_spread = dict(zip(ys["year"].to_list(), (ys["s"] * ann).to_list()))

    return {
        "decile_ann_ret": dict(zip(dec_mean["decile"].to_list(), dec_mean["ann_ret"].to_list())),
        "spread_ann": spread_ann, "t_spread": t_spread, "sharpe": sharpe,
        "top_excess_ann": top_excess_ann, "monotonic": mono, "year_spread": year_spread,
        "n_days": wide.height,
    }


def turnover(panel: pl.DataFrame, factor: str, n: int = 10, top: bool = True) -> dict:
    """目标分层（默认第 10 档）逐日持仓中，到下一交易日被换出的名字占比（等权前提下的近似换手）。"""
    d = panel.filter(pl.col("in_univ") & pl.col(factor).is_not_null())
    d = _add_decile(d, factor, n)
    target = n if top else 1
    sel = d.filter(pl.col("decile") == target).group_by("day", maintain_order=True).agg(
        pl.col("symbol").alias("syms")
    ).sort("day")
    syms = sel["syms"].to_list()
    fracs = []
    for i in range(1, len(syms)):
        prev = set(syms[i - 1])
        if not prev:
            continue
        fracs.append(len(prev - set(syms[i])) / len(prev))
    return {"avg_daily_turnover": float(np.mean(fracs)) if fracs else float("nan"), "n_days": len(syms)}


def neutralize(panel: pl.DataFrame, factor: str, controls: tuple[str, ...] = ("log_amt20", "mom20")) -> pl.DataFrame:
    """factor 对 controls 做逐日截面 OLS（用秩，不用原始量纲），返回新列 {factor}_neu = 残差。

    只在 in_univ 内做回归（更干净的截面），非 universe 行留 null；调用方按 (day, symbol) 关联回去。
    """
    seen = {factor}
    ctrl_list = []
    for c in controls:
        if c == factor or c in seen:
            continue
        if c not in panel.columns:
            continue
        seen.add(c)
        ctrl_list.append(c)
    cols = (factor, *ctrl_list)
    base = panel.filter(pl.col("in_univ")) if "in_univ" in panel.columns else panel
    d = base.select("day", "symbol", *cols).with_columns(
        [pl.col(c).rank().over("day").alias(f"__r_{c}") for c in cols]
    )
    rcols = [f"__r_{c}" for c in cols]
    out_name = f"{factor}_neu"

    def _resid(grp: pl.DataFrame) -> pl.DataFrame:
        y = grp[f"__r_{factor}"].to_numpy().astype(float)
        X = np.column_stack(
            [grp[f"__r_{c}"].to_numpy().astype(float) for c in controls] + [np.ones(len(grp))]
        )
        mask = ~np.isnan(y) & ~np.isnan(X).any(axis=1)
        resid = np.full(len(grp), np.nan)
        if mask.sum() > len(controls) + 1:
            beta, *_ = np.linalg.lstsq(X[mask], y[mask], rcond=None)
            resid[mask] = y[mask] - X[mask] @ beta
        return grp.with_columns(pl.Series(out_name, resid))

    res = d.group_by("day", maintain_order=True).map_groups(_resid)
    return panel.join(res.select("day", "symbol", out_name), on=("day", "symbol"), how="left")


def per_year_ic(panel: pl.DataFrame, battery_df: pl.DataFrame, top_n: int = 10) -> pl.DataFrame:
    """按 |ICIR| 排序的前 N 行展开成 (factor, horizon) × (year) 的逐年 IC 表。"""
    if battery_df.height == 0:
        return pl.DataFrame()
    top = battery_df.head(top_n)
    rows = []
    for f, h in zip(top["factor"].to_list(), top["horizon"].to_list()):
        if f not in panel.columns or h not in panel.columns:
            continue
        ic_df = rank_ic(panel, f, h)
        if ic_df.height == 0:
            continue
        icsum = ic_summary(ic_df, lag=_horizon_n(h))
        for yr, m in icsum["by_year"].items():
            rows.append({"factor": f, "horizon": h, "year": int(yr), "mean_ic": float(m)})
    if not rows:
        return pl.DataFrame()
    out = pl.DataFrame(rows)
    return out.pivot(values="mean_ic", index=["factor", "horizon"], on="year").sort("factor", "horizon")


def battery(
    panel: pl.DataFrame,
    factors: list[str],
    horizons: tuple[str, ...] = ("fwd_1", "fwd_5", "fwd_10", "fwd_20", "fwdo_5", "fwdo_10"),
) -> pl.DataFrame:
    """每个 (factor, horizon) 跑 IC / 分层收益 / 换手；原始因子 + 中性化因子各占一行。"""
    rows = []
    for f in factors:
        if f not in panel.columns:
            continue
        neu = neutralize(panel, f)
        fneu = f"{f}_neu"
        turn = turnover(neu, f)
        turn_neu = turnover(neu, fneu)
        for h in horizons:
            N = _horizon_n(h)
            for fac, turn_d in ((f, turn), (fneu, turn_neu)):
                ic_df = rank_ic(neu, fac, h)
                icsum = ic_summary(ic_df, lag=N)
                q = quantile_returns(neu, fac, h)
                gross = q["decile_ann_ret"].get(10, float("nan"))
                gross_bot = q["decile_ann_ret"].get(1, float("nan"))
                net_bot = gross_bot - (ANN_DAYS / N) * COST_PER_ROUND_TRIP if gross_bot == gross_bot else float("nan")
                t = turn_d["avg_daily_turnover"]
                # 持有 N 日、到期全换的成本上界：每年 244/N 次往返；日频换手率 t 只作参考列。
                net = gross - (ANN_DAYS / N) * COST_PER_ROUND_TRIP if gross == gross else float("nan")
                rows.append({
                    "factor": fac, "horizon": h, "mean_ic": icsum["mean"], "icir": icsum["icir"],
                    "t_nw": icsum["t_nw"], "pct_pos": icsum["pct_pos"],
                    "q_spread_ann": q["spread_ann"], "t_spread": q["t_spread"], "sharpe": q["sharpe"],
                    "top_decile_gross": gross, "turnover": t, "top_decile_net": net, "bot_decile_net": net_bot,
                    "monotonic": q["monotonic"], "n_days": icsum["n_days"],
                })
    out = pl.DataFrame(rows)
    if out.height == 0:
        return out
    # polars 的 nulls_last 对 float NaN 不生效（NaN 会被当作 -∞ 排到降序最前），把
    # NaN 先 fill 成 null 再排序；ICIR=null/NaN 通常意味着该 (factor, horizon) 整列没
    # 有效样本，放在最后就行。
    out = out.with_columns(_a=pl.col("icir").abs().fill_nan(None))
    out = out.sort("_a", descending=True, nulls_last=True).drop("_a")
    return out


def _fmt(v: object) -> str:
    if isinstance(v, float):
        return "nan" if v != v else f"{v:.4f}"
    return str(v)


def to_markdown(df: pl.DataFrame) -> str:
    if df.height == 0:
        return "(empty)"
    lines = ["| " + " | ".join(df.columns) + " |", "| " + " | ".join("---" for _ in df.columns) + " |"]
    for row in df.iter_rows():
        lines.append("| " + " | ".join(_fmt(v) for v in row) + " |")
    return "\n".join(lines)


def _default_factors(panel: pl.DataFrame) -> list[str]:
    numeric = {pl.Float32, pl.Float64, pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64}
    return [c for c in panel.columns if c not in NON_FACTOR_COLS and panel.schema[c] in numeric]


def main() -> int:
    import datetime as dt
    ap = argparse.ArgumentParser()
    ap.add_argument("--panels", nargs="+", type=Path, required=True)
    ap.add_argument("--factors", type=str, default=None)
    ap.add_argument("--horizons", type=str, default="fwd_1,fwd_5,fwd_10,fwd_20,fwdo_5,fwdo_10")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--start", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--end", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--min-amt", type=float, default=1e7)
    ap.add_argument("--min-age", type=int, default=60)
    a = ap.parse_args()

    panel = load_panel(a.panels)
    if a.start is not None:
        panel = panel.filter(pl.col("day") >= a.start)
    if a.end is not None:
        panel = panel.filter(pl.col("day") <= a.end)
    panel = add_returns(panel)
    panel = universe(panel, min_amt=a.min_amt, min_age=a.min_age)
    factors = [s.strip() for s in a.factors.split(",")] if a.factors else _default_factors(panel)
    horizons = tuple(a.horizons.split(","))

    n_days = panel.select("day").unique().height
    day_min, day_max = panel["day"].min(), panel["day"].max()
    print(f"calendar: {day_min} .. {day_max} ({n_days} days), {panel.select('symbol').unique().height} symbols")

    tab = battery(panel, factors, horizons=horizons)
    a.out.mkdir(parents=True, exist_ok=True)
    tab.write_parquet(a.out / "battery.parquet")
    md = to_markdown(tab)
    (a.out / "battery.md").write_text(md, encoding="utf-8")
    pyr = per_year_ic(panel, tab, top_n=10)
    if pyr.height:
        pyr.write_parquet(a.out / "battery_ic_by_year_top10.parquet")
        (a.out / "battery_ic_by_year_top10.md").write_text(to_markdown(pyr), encoding="utf-8")
    print(md)
    if pyr.height:
        print("\n--- per-year IC for top 10 by |ICIR| ---")
        print(to_markdown(pyr))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
