#!/usr/bin/env bash
# 本地与 CI 共用的门禁入口。任何一道失败即整体失败，不可跳过。
# 顺序按性价比：结构（秒级）→ 依赖方向 → 私有符号 → 词汇 → 行为（pytest）。
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src

run() { echo; echo "== $1"; shift; "$@"; }

run "ruff（复杂度 / 私有属性 / 死注释）" uv run ruff check src tests tools
run "import-linter（分层方向 / 纯逻辑核禁 IO）" uv run lint-imports
run "私有符号跨模块 import" uv run python tools/check_private_imports.py src
run "缺陷与形状账本（结构 / 枚举相等 / 语义 append-only；基线 = merge-base HEAD origin/main，不可覆盖）" uv run python tools/check_ledger.py
run "词汇（grep 找候选 → 语义判定，无豁免名单）" uv run python tools/check_vocab.py
run "pytest + 覆盖率" uv run pytest --cov=ftbv2 --cov-report=xml --cov-report=term
echo; echo "全部门禁通过"
