from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from factor_eval import add_returns, battery, ic_summary, quantile_returns, rank_ic, universe

N_SYM, N_DAY = 300, 300


def _synthetic_panel(seed: int = 0, drop: tuple[tuple[int, int], ...] = ()) -> pl.DataFrame:
    """300 symbols x 300 days random-walk panel. `drop` = (symbol_idx, day_idx) rows to omit
    entirely, simulating a full-day suspension (no row at all for that symbol/day).

    Same seed + same random-walk logic always produces the same close-price series regardless
    of `drop`, so calling this twice (once with a drop, once without) gives a ground-truth
    price path to check forward returns against.
    """
    rng = np.random.default_rng(seed)
    symbols = [f"{i:06d}.SZ" for i in range(N_SYM)]
    calendar = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(N_DAY)]
    drop_set = set(drop)

    rows = []
    for si, sym in enumerate(symbols):
        logret = rng.normal(0.0, 0.02, N_DAY)
        close = 100_000 * np.exp(np.cumsum(logret))  # 元x10000, ~10 元起
        close = np.round(close).astype(np.int64)
        # 交易所语义：停牌日没有收盘价，复牌日的前收盘 = 停牌前最后一个真实收盘（不是被删掉那天的 close）
        prev_close = np.empty(N_DAY, dtype=np.int64)
        prev_close[0] = close[0]
        last = close[0]
        for di in range(1, N_DAY):
            prev_close[di] = last
            if (si, di) not in drop_set:
                last = close[di]
        open_ = np.empty(N_DAY, dtype=np.int64)
        open_[0] = close[0]
        open_[1:] = np.round(close[:-1] * (1 + rng.normal(0, 0.005, N_DAY - 1))).astype(np.int64)
        amt = rng.uniform(3e7, 6e7, N_DAY)
        for di in range(N_DAY):
            if (si, di) in drop_set:
                continue
            rows.append({
                "day": calendar[di], "symbol": sym,
                "close": int(close[di]), "open": int(open_[di]),
                "high": int(max(close[di], open_[di]) * 1.01), "low": int(min(close[di], open_[di]) * 0.99),
                "prev_close": int(prev_close[di]),
                "vol": 1_000_000, "amt": float(amt[di]),
                "n_snap": 4800, "frac_no_ask": 0.0, "frac_no_bid": 0.0,
                "amt_at_1445": amt[di] * 0.85, "amt_at_0945": amt[di] * 0.1,
                "amt_at_1457": amt[di] * 0.95, "amt_at_0930": 0.0,
            })
    return pl.DataFrame(rows).with_columns(pl.col("day").cast(pl.Date))


@pytest.fixture(scope="module")
def panel() -> pl.DataFrame:
    p = add_returns(_synthetic_panel())
    return universe(p, min_amt=1e7, min_age=60)


def test_factor_a_strong_signal_has_positive_ic(panel: pl.DataFrame) -> None:
    rng = np.random.default_rng(1)
    p = panel.with_columns(
        A=pl.col("fwd_5") + pl.Series(rng.normal(0, 0.01, panel.height))
    )
    ic = rank_ic(p, "A", "fwd_5")
    summary = ic_summary(ic, lag=5)
    assert summary["n_days"] > 100
    assert summary["mean"] > 0.3
    q = quantile_returns(p, "A", "fwd_5")
    assert q["monotonic"] > 0.8


def test_factor_b_pure_noise_has_near_zero_ic(panel: pl.DataFrame) -> None:
    rng = np.random.default_rng(2)
    p = panel.with_columns(B=pl.Series(rng.normal(0, 1, panel.height)))
    ic = rank_ic(p, "B", "fwd_5")
    summary = ic_summary(ic, lag=5)
    assert abs(summary["mean"]) < 0.02


def test_battery_runs_and_sorts_by_abs_icir(panel: pl.DataFrame) -> None:
    rng = np.random.default_rng(3)
    p = panel.with_columns(
        A=pl.col("fwd_5") + pl.Series(rng.normal(0, 0.01, panel.height)),
        B=pl.Series(rng.normal(0, 1, panel.height)),
    )
    tab = battery(p, ["A", "B"], horizons=("fwd_5",))
    assert tab.height == 4  # 2 factors x (raw, neutralized) x 1 horizon
    icirs = tab["icir"].abs().to_list()
    assert icirs == sorted(icirs, reverse=True)


def test_forward_return_matches_direct_computation_across_missing_day() -> None:
    # symbol 0 is missing on day index 150 (full-day suspension: no row at all).
    sym_idx, missing_day = 0, 150
    raw = _synthetic_panel(seed=7, drop=((sym_idx, missing_day),))
    # ground truth close series (from an undropped generation with the same seed/logic)
    full_raw = _synthetic_panel(seed=7, drop=())
    truth = full_raw.filter(pl.col("symbol") == f"{sym_idx:06d}.SZ").sort("day")
    close_truth = dict(zip(truth["day"].to_list(), truth["close"].to_list()))

    p = add_returns(raw)
    sym = f"{sym_idx:06d}.SZ"
    sub = p.filter(pl.col("symbol") == sym).sort("day")
    days = sub["day"].to_list()
    fwd5 = sub["fwd_5"].to_list()

    t = missing_day - 3  # window [t+1, t+5] spans the missing day
    day_t = days[t]
    day_t5 = days[t + 5]
    expected = close_truth[day_t5] / close_truth[day_t] - 1
    assert fwd5[t] == pytest.approx(expected, rel=1e-9)

    # the missing day's own row must have all factor/return columns null (present via full grid)
    missing_row = sub.filter(pl.col("day") == days[missing_day])
    assert missing_row.height == 1
    assert missing_row["close"].item() is None
    assert missing_row["ret1"].item() is None


def test_close_zero_treated_as_missing() -> None:
    raw = _synthetic_panel(seed=5)
    sym = "000000.SZ"
    # simulate an all-day-suspension row that was still written with close=0 (per data note).
    idx = raw.with_row_index().filter((pl.col("symbol") == sym) & (pl.col("day") == raw["day"][10])).select(
        "index"
    ).item()
    raw = raw.with_columns(
        pl.when(pl.arange(0, pl.len()) == idx).then(0).otherwise(pl.col("close")).alias("close")
    )
    p = add_returns(raw)
    row = p.filter((pl.col("symbol") == sym) & (pl.col("day") == raw["day"][10]))
    assert row["ret1"].item() is None
    assert row["close"].item() is None
