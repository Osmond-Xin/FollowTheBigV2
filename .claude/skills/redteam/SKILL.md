---
name: redteam
description: 三方异构红队（opencode/MiniMax · agy/Gemini · codex/OpenAI 三个 CLI，三个攻击面并行）。用于：设计简报定稿前的红队；一个模块或一批代码写完后的加固 review（附 crapkit ratchet）。
---

# 三方红队

同一份对象，三个异构模型各攻一面，独立落盘。**三方独立命中**的要害最可信；单方命中要自己核实。

| runner | 模型 | 攻击面 | 读仓库 |
|---|---|---|---|
| opencode（默认） | MiniMax-M3，plan agent 只读 | 方法论：判据、样本、多重检验、事件/因子纯度、数值语义 | 是 |
| mmx（可选，与 opencode 二选一） | MiniMax 纯文本 CLI | 同上 | 否——脚本把 CONTEXT.md 注入提示词。留给日后「对照裁决」这种不该看项目历史的场合 |
| agy | Gemini，plan 模式 | 架构：接口完整性、缝、纯/IO 分半、旋钮、三份活文档矛盾 | 是 |
| codex | OpenAI，read-only 沙箱 | 工程：安全、健壮、正确性、门禁可绕过性、退化路径 | 是 |

攻击面提示词在 `tools/redteam/lens/`；改攻击面改那里，不改脚本。脚本是 fail-closed 的：任一 runner 执行失败、无有效裁决、或 crapkit 失败都退出 1。

## 步骤

1. **选模式并运行**（三路并行，几分钟）。
   - 设计红队：`bash tools/redteam/redteam.sh design <brief.md> <tag>` → `docs/design-log/<日期>-红队-<tag>-{方法论,架构,工程}.md` + manifest
   - 加固 review：`bash tools/redteam/redteam.sh review [<base-ref>]`（默认 `main`，含未提交改动、新文件、活文档改动的纯文本抽取）→ `.redteam/<时间>-<sha>-<rand>/{opencode,agy,codex}.md` + `crapkit.md` + `manifest.json`
   - 只跑子集：`REDTEAM_ONLY=opencode,codex`。对象超过 200 KB 会拒绝：拆 commit，不要调上限。
   - 完成判据：脚本打印每方裁决；退出码 0 当且仅当每方都是「裁决：通过」且 crapkit verify OK。
2. **逐份读完全部输出**，按「证据 → 攻击路径 → 修正」核实每条。完成判据：每条发现都归入四类之一——三方独立命中 / 单方命中且核实成立 / 单方命中但不成立（写一句为什么）/ 攻击面错套（例如对工具脚本套统计框架，或把「下一步才建的机制」当成本次缺陷）。
3. **处理**。致命与严重项改完再跑一次 review；设计红队的采纳项先改 CONTEXT.md → 架构图 → 数据表，再归档红队原文。
4. **汇报**：三方各自的裁决、采纳了哪些、拒绝了哪些及理由、crapkit 是否 verify OK。

## 已知的坑

- crapkit 的 ratchet 基线是在零业务代码时冻的（`crapkit-ratchet.tsv` 为空）；第一批纯核代码合入后要 `uv run crapkit coverage && uv run crapkit ratchet seed` 重冻，否则 ratchet 拦不住任何东西。
- macOS 自带 bash 3.2：脚本里不能用关联数组；`$var` 后紧跟中文全角字符会被吞进变量名，一律写 `${var}`。
- opencode 的 `-f` 是数组参数，message 必须写在 `-f` 之前；agy 只接受 argv 提示词；codex 后台化时 stdin 必须 `- < prompt` 显式喂。
- 模型常把裁决与总结写在同一行，脚本取末尾三个非空行里最后一次匹配。
- 对象上限 200 KB 是硬的：相对 main 的 diff 太大时（例如整条功能分支），把源码打包成简报走 `design` 模式，测试与文档留在仓库里让 agentic 两路自己读。
- agy 无头模式读不了文件也写不了文件：对象必须内联，让它回传代码块；opencode plan agent 同理，要它给代码要明说「不要调用 bash」。
- MiniMax 的结构化输出若以 `[` 开头会只回一个 `[` 就停，要它输出 JSON 时用对象包裹。
