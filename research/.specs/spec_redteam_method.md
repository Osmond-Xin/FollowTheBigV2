你是方法论红队。对象：仓库 research/ 目录下的一条 A 股 Level-2 因子研究流水线，以及它得出的结论（docs/design-log/2026-09-03-探索-赚钱模式-预注册.md 第六到第八节）。
请只读不改。重点找会让结论作废的东西：
1. 前视偏差：research/panel/flow_day.py 与 research/panel/daily_ref.py 算的 (日, 标的) 特征是否只用了当日 15:00 之前的数据；research/eval/factor_eval.py 里 add_returns / universe 是否用了未来信息选样本（例如用 t+1 的开盘价判断可交易，是否会制造偏差）；neutralize 是否用了未来。
2. 幸存者偏差与宇宙：面板来自当天真实存在的标的（主板前缀筛选），退市股会不会在退市前就消失；停牌处理；close==0 的处理。
3. 复权：远期收益用 close/prev_close 连乘，prev_close 是交易所前收盘；这在送转、配股、除息时是否正确。
4. 统计：每日截面 Spearman IC、Newey-West t、分组年化（×244/N）、取样步长 3 天的日子做 IC 是否会让 t 值虚高（重叠持有期）。
5. 因子构造的机制风险：qf_lot1_b（100 股买委托占比）、t_sign_ac1（成交方向 lag-1 自相关）、pre_cx_b（9:15–9:20 委托在 9:25 前撤单比例）、rkurt（5 分钟收益峰度）——是否有明显的股价水平 / 流动性 / 涨跌停 / ST 混杂没被 log_close、log_amt20、mom20、ret1、vol20 的中性化吸收；上交所委托量是撮合后残量、深交所是原始量，这对 qf_lot1_b 的沪深可比性有什么影响。
6. 代码 bug：任何会系统性抬高 IC 的实现错误。
输出：中文；按【致命/严重/建议】列出，每条带 file:line 证据、攻击路径、修正建议；最后一行写「裁决：通过 / 需改 / 不得合并」。不要客套，不要复述。
