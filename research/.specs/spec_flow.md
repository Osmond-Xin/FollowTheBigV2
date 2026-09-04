You are working in the repo /Users/osmond/Documents/project/FollowTheBigV2 (branch explore/赚钱模式). Python 3.12, run everything with `PYTHONPATH=src uv run python ...`. polars, pyarrow, numpy, pandas, scipy are installed. Do NOT modify anything under src/ (read it freely). Do NOT run git commit. Do NOT run tools/gate.sh. Do not touch research/eval/.

## Task
Write `research/panel/flow_day.py`: for each trading day, read the A-share Level-2 逐笔成交 (trades) and 逐笔委托 (orders) streams via the existing `RawStore` and write ONE parquet with one row per (day, symbol) containing the daily microstructure features listed below. Output dir: `/Volumes/xin/FollowTheBigV2-derived/flow/date=YYYYMMDD.parquet` (write `.tmp` then rename; skip if exists). CLI: `--out DIR --workers N --days YYYY-MM-DD ... --reverse` — same shape as `research/panel/daily_ref.py`; read it first and copy its structure (multiprocessing spawn Pool, imap_unordered, per-worker RawStore).

## How to read data (read these files first)
- `src/ftbv2/core/raw/schema.py` (FIELDS, session constants AM_START_MS etc, PRICE_SCALE=10000: price int = 元×10000)
- `src/ftbv2/core/raw/types.py` (ReadRequest, Window), `src/ftbv2/io/raw/store.py` (RawStore.execute)
- `research/panel/daily_ref.py` (working example)
```python
from ftbv2.core.raw import ReadRequest, parse_ledger, plan
from ftbv2.io.raw import RawStore
ledger = parse_ledger(Path('ledger/defects.toml').read_text(encoding='utf-8'))
st = RawStore(Path('/Volumes/辛的硬盘/data/preserve'), ledger)
res = st.execute(plan(ReadRequest('trades', (day,), ('time_ms','code','bs','price','vol','ask_ref','bid_ref')), st.catalog('trades',(day,)), ledger))
df = res.frame   # columns: day, symbol + requested fields; time_ms = ms since midnight; ints are Int64
```
Orders fields: ('time_ms','oid','type','side','price','vol'). A full day is ~100M trade rows and ~118M order rows; each read takes ~20–26 s. Machine has 64 GB RAM: select only needed columns, process trades then orders, `del` frames when done, default `--workers 3`. Note: a background job is currently reading the same disk, so timings will be inflated.

## Data semantics (verified on 2025-03-12; re-verify anything you rely on with a quick group_by on that day)
- Suffix `.SZ` 深交所 / `.SH` 上交所. They differ:
- SZ trades: `code`='0' is a fill, `bs` in {B,S} = aggressor side (B = active buy). `code`='C' is a CANCEL row: bs=' ', price=0, vol = cancelled qty, exactly one of ask_ref/bid_ref is nonzero = the cancelled order's oid (bid_ref → a buy order cancelled). Fills carry both ask_ref and bid_ref = the two order ids; the aggressor is the order with the LARGER oid (later arrival). Cancel rows exist also before 09:25 (auction period).
- SH trades: `code` is '\x00' for every row (not null, not empty). bs in {B,S} — verify the meaning on SH (check the price relation to neighbouring trades); document what you find. ask_ref/bid_ref are order ids on SH too. SH has no cancel rows in trades.
- SZ orders: type '0' limit, '1' market, 'U' 本方最优; side B/S; vol = ORIGINAL order quantity.
- SH orders: type 'A' = new order, 'D' = cancel (vol = cancelled qty, oid = the cancelled order); vol on 'A' rows is the REMAINING quantity after immediate matching, and fully-filled aggressive orders never appear as 'A' rows. Compute the same features anyway but keep a `mkt` column ('SH'/'SZ').
- Both streams contain 1 junk row per symbol with price=0 & vol=0 & empty type/side. Drop rows with vol<=0 (cancel rows have price 0 but vol>0 — keep them).
- Sessions: 开盘集合竞价 [09:15,09:25); 连续竞价 [09:30,11:30) ∪ [13:00,14:57); 收盘集合竞价 [14:57,15:00]. Trades stamped 09:25:00 are the opening-auction fills; 15:00:00 the closing-auction fills.
- Money: amt = price/10000 * vol (元).

## Features (one row per day, symbol; Float64; null when undefined)
Keys: day, symbol, mkt.
Trades (continuous session unless noted):
1. `t_buy_amt, t_sell_amt, t_n_buy, t_n_sell` aggressor totals; `t_imb = (buy-sell)/(buy+sell)`.
2. `t_imb_first30` [09:30,10:00), `t_imb_last30` [14:27,14:57), `t_imb_open_call` (fills at 09:25), `t_imb_close_call` (fills ≥14:57). `t_amt_share_first30, t_amt_share_last30, t_amt_share_close_call, t_amt_share_open_call` = window amt / whole-day amt.
3. Order-level aggregation: group fills by aggressor oid (per symbol) → order fill amt; buckets: b1 <2万, b2 [2万,5万), b3 [5万,20万), b4 [20万,100万), b5 ≥100万. For each bucket k: `o_net_k = (buy amt − sell amt)/day amt`, `o_share_k = bucket amt / day amt`. Same on the PASSIVE side (group by the resting order id; a resting buy order filled = passive buying) → `p_net_k`, `p_share_k`.
4. Row-level trade-size buckets (classic version): `r_net_k`, same thresholds.
5. Fingerprints on aggressor orders: `f_round500` = share of aggressor-order amt whose total vol is a multiple of 500 and ≥500; `f_round1000` same for 1000; `f_lot1` = share (count) of aggressor orders with vol == 100.
6. `t_sign_ac1` = lag-1 autocorrelation of trade sign (+1 buy, −1 sell) in the continuous session; `t_run_mean` = mean run length of same-sign trades.
7. From trade prices sampled at the last trade of each 5-minute bin in the continuous session: `rv` = sqrt(sum of squared log returns), `rskew`, `rkurt`, `kyle_lambda` = OLS slope of 5-min log return on 5-min signed net amt (buy−sell, 万元), `amihud = |last/first − 1| / day amt` (first & last trade prices).
8. SZ cancels from trades (all day): `c_n_bid, c_n_ask, c_vol_bid, c_vol_ask`.
Orders:
9. `q_n_b, q_n_s, q_amt_b, q_amt_s` (continuous session, real orders only); `q_mean_amt_b, q_mean_amt_s`; `q_big20_share_b` = share of buy-order amt from orders ≥20万; `q_big100_share_b` for ≥100万; same `_s`.
10. Cancels joined to orders by oid (SZ: cancel rows in trades; SH: 'D' rows in orders): `life_ms = cancel time − order time`. `cx_rate_b = cancelled buy orders / buy orders placed` (count, all day), `cx_rate_s`; `cx_hf_b` = buy orders cancelled with life_ms < 1000 / buy orders placed, `cx_hf_s`; `cx_amt_rate_b` (cancelled amt / placed amt), `_s`; `cx_big_rate_b` = cancel rate among buy orders ≥20万 (count).
11. Pre-open: `pre_cx_b` = buy orders placed in [09:15,09:20) cancelled before 09:25 / buy orders placed in [09:15,09:25); `pre_cx_s`; `pre_n_b, pre_n_s, pre_amt_b, pre_amt_s`.
12. Order fingerprints: `qf_round500_b` = share of buy-order amt with vol%500==0 and vol≥500; `qf_lot1_b` = share (count) of buy orders with vol==100; same `_s`.
13. `q_life_med_b` median life_ms of cancelled buy orders; `_s`.

## Deliverables
1. `research/panel/flow_day.py` with the CLI; pure polars functions `features_trades(df)` and `features_orders(orders, cancels)` joined on (day, symbol). No Python row loops.
2. Verify on 2025-03-12 for ALL symbols: print wall time per stream and total; print the transposed feature rows for 600519.SH and 000001.SZ; null counts per column; invariants: t_buy_amt + t_sell_amt vs `amt` in `/Volumes/xin/FollowTheBigV2-derived/daily_ref/date=20250312.parquet` (report ratio distribution; continuous-session vs whole-day explains part), o_share buckets sum ≈ 1, rates in [0,1]. Also run 2022-01-05 and report.
3. Target ≤ 120 s/day/worker; profile if slower.
4. Write `research/panel/README.md` (Chinese, short): one line per column, SH-vs-SZ caveats you verified, and the exact command for the full run. Do NOT launch the full run.
Finish with a concise report: what works, timings, anomalies, anything unverified.
