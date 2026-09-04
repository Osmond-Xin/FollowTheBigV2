"""红队回应（2026-09-04）：确认集 2022–2023 上 (1) 经验置换检验 (2) 去 qf_lot1_b 版本 (3) 分市场 IC (4) 分量秩相关。"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import polars as pl
from factor_eval import add_returns, load_panel, universe, rank_ic, ic_summary
from composite import SPECS, composite

P = [Path('/Volumes/xin/FollowTheBigV2-derived/daily_ref'), Path('/Volumes/xin/FollowTheBigV2-derived/flow')]
p = universe(add_returns(load_panel(P))).filter((pl.col('day') >= pl.date(2022, 1, 1)) & (pl.col('day') <= pl.date(2023, 12, 31)))
H = 'fwdo_10'
rng = np.random.default_rng(0)
for spec in ('algo_footprint', 'algo_footprint_nq', 'core', 'core_nq'):
    q = composite(p, SPECS[spec], 'score', neu=True)
    s = ic_summary(rank_ic(q, 'score', H), lag=10)
    line = f"{spec:18s} 确认集 {H}: IC {s['mean']:.4f} ICIR {s['icir']:.2f} NW-t {s['t_nw']:.1f} n {s['n_days']}"
    for m in ('SH', 'SZ'):
        sm = ic_summary(rank_ic(q.filter(pl.col('symbol').str.ends_with(m)), 'score', H), lag=10)
        line += f" | {m} IC {sm['mean']:.4f} t {sm['t_nw']:.1f}"
    print(line, flush=True)
    if spec in ('algo_footprint', 'algo_footprint_nq'):
        # 置换：把每个信号日的整张截面分数配给随机另一天的收益（保留分数的截面结构，打断时间对齐），500 次
        base = q.filter(pl.col('in_univ_exec') & pl.col('score').is_not_null() & pl.col(H).is_not_null()).select('day', 'symbol', 'score', H)
        days = sorted(base['day'].unique().to_list()); obs = s['mean']
        sc = base.select('day', 'symbol', 'score'); rt = base.select('day', 'symbol', H)
        null = []
        for _ in range(500):
            perm = dict(zip(days, rng.permutation(days)))
            shifted = sc.with_columns(pl.col('day').replace_strict(perm, default=None)).drop_nulls('day')
            j = shifted.join(rt, on=['day', 'symbol'], how='inner')
            j = j.with_columns(a=pl.col('score').rank().over('day'), b=pl.col(H).rank().over('day'))
            null.append(float(j.group_by('day').agg(pl.corr('a', 'b').alias('c'))['c'].drop_nans().mean()))
        null = np.array(null)
        print(f"  置换 500 次：观测 IC {obs:.4f}；零分布均值 {null.mean():.4f} 标准差 {null.std():.4f} 最大 {null.max():.4f}；经验 p = {(null >= obs).mean():.3f}", flush=True)
# 分量秩相关（确认集，日内秩，池化）
comps = ['qf_lot1_b', 'f_lot1', 't_sign_ac1', 'q_life_med_b', 'r_share_b2', 't_amt_share_close_call', 'pre_cx_b', 'rkurt']
r = p.filter(pl.col('in_univ')).select(['day'] + comps).with_columns([pl.col(c).rank().over('day') for c in comps]).drop('day').drop_nulls()
m = np.corrcoef(r.to_numpy().T)
print('分量秩相关矩阵（确认集）:'); print(pl.DataFrame(np.round(m, 2), schema=comps).with_columns(pl.Series('_', comps)).select(['_'] + comps))
