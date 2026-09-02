#!/usr/bin/env bash
# 禁用词门禁。规则与豁免的定义在 CONTEXT.md 第三节「因子」条；README 里是同一条命令。
# 「特征」是禁用词，一律说「因子」。豁免：CONTEXT.md（规则自身）、design-log（归档原文）、复合词「性能特征」。
set -u
cd "$(dirname "$0")/.."
hits=$(grep -rn '特征' --exclude=CONTEXT.md --exclude-dir=design-log --exclude-dir=.venv --exclude-dir=.git . | grep -v '性能特征' || true)
if [ -n "$hits" ]; then
  echo "禁用词命中："
  echo "$hits"
  exit 1
fi
echo "禁用词门禁：通过"
