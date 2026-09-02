# FollowTheBigV2

FollowTheBig（V1）的推倒重建。研究目标不变——从 A 股 Level-2 逐笔数据里找「正在准备拉升的庄」；
工程全部重来。V1 被判定为工程失败（`scripts/`:`src/` = 24:1，684 个脚本 473 个从未被 import，
路径字面量 777 处，`read_parquet` 直接调用 1,123 次，113 万词文档）。

## 唯一裁决依据（三份活文档 + 词汇源）

| 文件 | 管什么 | 规则 |
|---|---|---|
| `CONTEXT.md` | **术语的源**。只定义词，不含实现、决策、状态 | 冲突时以它为准；先改它再改别处 |
| `docs/词汇表.html` | `CONTEXT.md` 的渲染 | ⚠️ 目前手写同步，**待改为脚本生成** |
| `docs/架构图.html` | 模块、缝、门禁线、事件层两层结构 | **图上没有的模块，视为不存在** |
| `docs/数据表.html` | 两块盘上存了什么、每列语义、缺陷账本、V1 公式、事件层 | **表里没有的数据，视为不存在** |

`docs/design-log/` 是**归档的过程文档**（含三方红队原文）。可读，不作裁决依据。

## 现在处于什么阶段

**设计阶段收口，仓库与门禁已立，尚未写一行业务代码。** 所有设计决策（Q1–Q18 + 红队后九条）见 `design-log/2026-09-01-立项讨论.md`；
深模块评审的四条高优先级已采纳（`design-log/2026-09-01-深模块评审.md`），中低项待裁决。

- 仓库：`github.com/Osmond-Xin/FollowTheBigV2`（私有）。**分支保护需要 GitHub Pro 或转公开**，目前 main 未受保护。
- 包：`src/ftbv2/core`（纯逻辑核）/ `src/ftbv2/io`（IO 层）。import-linter 硬拒 core → io。
- 门禁：`bash tools/gate.sh`（ruff · import-linter · 私有 import · 词汇 · pytest），CI 同一入口（`.github/workflows/gate.yml`）。
  词汇门禁需要 `MINIMAX_API_KEY`（本机读 `~/.mmx/config.json`，CI 读仓库 secret）；判定不可用即门禁失败。
- CRAP：`crapkit.toml`，只用 ratchet（基线已冻）。`uv run crapkit verify`。
- 红队：`/redteam` skill → `tools/redteam/redteam.sh`，mmx / agy / codex 三路并行，攻击面在 `tools/redteam/lens/`。

**卡点**：三个必测数字——事件流实际体积 / 一次全量提取耗时 / 「宽而中性」的宽度——
20 个交易日、1 个 stream 跑一遍即得。**但机器上可能有 V1 的生产作业在读同一块 USB 盘**
（`rebuild_keep_locked.py`），且项目有 kernel panic 前科。**动盘前先 `ps`。**

**第一步**（已定）：codex 窄实验工厂——3–5 天扫描闭环 / 1–2 周账本进 CI / 3–4 周第一个假设。
此前不建多 agent 流水线、不建收据、不建 runner 编排。

## 硬规则速查

- 纯逻辑核与 IO 层必须分属不同模块（门禁倒逼，非风格）
- 事件不含判断；因子含判断且是状态机 `step(状态, 事件) → (新状态, 值?)`
- 结论落证据包 `.lineage/<id>/receipt.json`，**绝不写回代码注释**
- 预注册 append-only；样本宇宙属于预注册；阈值只属于预注册
- 裁决用经验置换检验，三态 + 附加标签；接口上没有旋钮
- 同一个概念只许一个词。**没有禁用词，没有豁免名单**：`tools/check_vocab.py` 按 CONTEXT.md 第八节的易混淆词表 grep 找候选，再由便宜的模型判定语义，并对改动文件查有没有引入与已有定义重叠的新定义
- 不得声称原始层「逐行无损」——77% 的交易日无法验证
- V1 派生库一个字节都不带进来；V1 结论（正负）全部待复验；V1 只是灵感来源

## 数据在哪

- 原始层 2.585 TB：`/Volumes/辛的硬盘/data/preserve/{orders,trades,xinqing}/`，exFAT，只读，4 IOPS
- 幸存 7z 685 GB（2022 全年 + 202608）：`/Volumes/xin/Level2/`，这块盘还有 1.1 TB 空闲且快 2×
- 内置 SSD 只剩 148 GB（`df -h /` 的 12Gi 是快照卷，别信）
- 全部列名是 `column_N`、全部 dtype 是 `large_string`，语义见数据表第三节
