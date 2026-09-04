"""codex 红队回应（2026-09-04）：前收盘复权语义实证 · 远期缺价率按分组 · 整块平移置换 · 撤前视后的确认集 IC。"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import polars as pl
from factor_eval import add_returns, load_panel, universe, rank_ic, ic_summary
from composite import SPECS, composite

P = [Path('/Volumes/xin/FollowTheBigV2-derived/daily_ref'), Path('/Volumes/xin/FollowTheBigV2-derived/flow')]
raw = load_panel(P)
# (6) 前收盘语义：prev_close_t 与 close_{t-1}（同一标的、按日历前一交易日）不等的比例与幅度
cal = sorted(raw['day'].unique().to_list()); nxt = dict(zip(cal[:-1], cal[1:]))
a = raw.select('day', 'symbol', 'close').filter(pl.col('close') > 0).with_columns(day=pl.col('day').replace_strict(nxt, default=None)).drop_nulls('day').rename({'close': 'close_prev'})
b = raw.select('day', 'symbol', 'prev_close').filter(pl.col('prev_close') > 0)
j = a.join(b, on=['day', 'symbol']).with_columns(ratio=pl.col('prev_close') / pl.col('close_prev'))
diff = j.filter((pl.col('ratio') - 1).abs() > 1e-6)
print(f"前收盘 ≠ 前一日收盘 的标的日占比 {diff.height / j.height:.4%}（{diff.height} / {j.height}）")
print('不等时的 ratio 分位:', [round(x, 4) for x in diff['ratio'].quantile(q) for q in ()] if False else diff.select(pl.col('ratio').quantile(0.01).alias('p1'), pl.col('ratio').quantile(0.1).alias('p10'), pl.col('ratio').median().alias('p50'), pl.col('ratio').quantile(0.9).alias('p90'), pl.col('ratio').quantile(0.99).alias('p99')))
print('ratio ≈ 0.5（10 送 10）的例子数:', diff.filter((pl.col('ratio') - 0.5).abs() < 0.01).height, '；ratio 在 (0.95,1) 的（除息）:', diff.filter((pl.col('ratio') > 0.95) & (pl.col('ratio') < 1)).height)
print(diff.sort('ratio').head(3)); print(diff.filter((pl.col('ratio') > 0.95) & (pl.col('ratio') < 1)).head(3))

p = universe(add_returns(raw))
conf = p.filter((pl.col('day') >= pl.date(2022, 1, 1)) & (pl.col('day') <= pl.date(2023, 12, 31)))
q = composite(conf, SPECS['algo_footprint_nq'], 'score', neu=True)
H = 'fwdo_10'
s = ic_summary(rank_ic(q, 'score', H), lag=10); print(f"撤前视后 algo_footprint_nq 确认集 {H}: IC {s['mean']:.4f} NW-t {s['t_nw']:.1f} n {s['n_days']}")
# (2) 远期缺价率按分组
d = q.filter(pl.col('in_univ_exec') & pl.col('score').is_not_null()).with_columns(dec=(pl.col('score').rank().over('day') / pl.col('score').count().over('day') * 10).ceil().clip(1, 10))
print('远期 fwdo_10 缺失率按分组（1=低,10=高）:'); print(d.group_by('dec').agg(miss=pl.col(H).is_null().mean(), n=pl.len()).sort('dec').transpose(include_header=True))
# (5) 整块平移置换：每个标的的分数序列按随机 ≥60 个信号日循环平移（保留标的静态结构与自相关，打断与收益的时间对齐）
base = q.filter(pl.col('in_univ_exec') & pl.col('score').is_not_null() & pl.col(H).is_not_null()).select('day', 'symbol', 'score', H).sort('symbol', 'day')
rng = np.random.default_rng(1); null = []
n_by = base.group_by('symbol').len()
for _ in range(200):
    k = rng.integers(60, 300)
    sh = base.with_columns(score=pl.col('score').shift(k).over('symbol')).with_columns(  # 循环：前 k 个用尾部补
        score=pl.when(pl.col('score').is_null()).then(pl.col('score').shift(-len(cal)).over('symbol')).otherwise(pl.col('score')))
    sh = base.with_columns(score=pl.concat_list([]).alias('_') if False else pl.col('score')).drop('score').join(
        base.select('symbol', 'day', 'score').with_columns(idx=pl.int_range(pl.len()).over('symbol')).with_columns(
            idx=(pl.col('idx') + k) % pl.col('idx').count().over('symbol')).select('symbol', 'idx', 'score'),
        on=['symbol', 'idx'], how='inner') if False else None
    # 简洁实现：按标的内位置循环平移
    tmp = base.with_columns(idx=pl.int_range(pl.len()).over('symbol'), n=pl.len().over('symbol'))
    scores = tmp.select('symbol', idx2=((pl.col('idx') + k) % pl.col('n')), score2=pl.col('score'))
    tmp = tmp.join(scores, left_on=['symbol', 'idx'], right_on=['symbol', 'idx2'], how='inner')
    tmp = tmp.with_columns(a=pl.col('score2').rank().over('day'), b=pl.col(H).rank().over('day'))
    null.append(float(tmp.group_by('day').agg(pl.corr('a', 'b').alias('c'))['c'].drop_nans().mean()))
null = np.array(null)
print(f"整块循环平移置换 200 次（每标的平移 60–300 个信号日）：观测 {s['mean']:.4f}；零分布均值 {null.mean():.4f} 标准差 {null.std():.4f} 最大 {null.max():.4f}；经验 p = {(null >= s['mean']).mean():.3f}")
# 日内截面打乱（作为参照，预期 ≈ 0）
null2 = []
for _ in range(100):
    tmp = base.with_columns(score2=pl.col('score').shuffle(seed=int(rng.integers(1e9))).over('day'))
    tmp = tmp.with_columns(a=pl.col('score2').rank().over('day'), b=pl.col(H).rank().over('day'))
    null2.append(float(tmp.group_by('day').agg(pl.corr('a', 'b').alias('c'))['c'].drop_nans().mean()))
print(f"日内截面打乱 100 次：零分布均值 {np.mean(null2):.4f} 标准差 {np.std(null2):.4f}")
