"""缺陷与形状账本门禁（工程管束 R3）：结构校验 + 代码枚举与账本 code 集合相等 + 语义 append-only。

语义 append-only 按 id 比较结构化条目，不看文本 diff（红队三方独立命中：`git diff` 可被重排 / rename / squash 绕过）：
- 禁删 id；禁改 code / kind / stream / created_at / evidence；days 只许并集；
- 状态只许 pending → active → superseded；superseded 必须 superseded_by 指向已存在 id，且一旦设置不可改；
- note 可改。
另：`ledger/observed/*.toml`（工具扫描的观测集合，可选）里的 code 必须 ⊆ 账本已登记 code。

用法：python tools/check_ledger.py [--base origin/main] [ledger/defects.toml]
退出码 0 = 通过；1 = 违规（逐条打印）；2 = 基线不可得（fail-closed）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

from ftbv2.core.raw.ledger import DefectCode, parse_ledger

IMMUTABLE = ("code", "kind", "stream", "created_at", "evidence")
TRANSITIONS = {("pending", "active"), ("active", "superseded"), ("pending", "superseded")}


def validate(text: str) -> list[str]:
    """结构 + 枚举相等。parse_ledger 抛的 ValueError 也算一条违规。"""
    try:
        ledger = parse_ledger(text)
    except (ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        return [f"账本解析失败：{exc}"]
    codes = {d.code for d in ledger.entries}
    problems = []
    if codes != set(DefectCode):
        only_code = sorted(c.value for c in set(DefectCode) - codes)
        only_ledger = sorted(c.value for c in codes - set(DefectCode))
        problems.append(f"代码枚举与账本 code 集合不相等：只在代码 {only_code}，只在账本 {only_ledger}")
    return problems


def _rows(text: str) -> dict[str, dict]:
    """基线与当前都按原始 TOML 字典比较：基线可能是旧结构（缺新字段），不能用严格解析器读它。"""
    rows = tomllib.loads(text).get("defect", [])
    return {str(r["id"]): r for r in rows if "id" in r}


def _days(row: dict) -> set[str]:
    return {str(d) for d in row.get("days", [])}


def compare(old_text: str, new_text: str) -> list[str]:
    """语义 append-only：old 是基线（origin/main），new 是当前。基线里没有的字段不比（结构迁移只能加字段）。"""
    old, new = _rows(old_text), _rows(new_text)
    problems = []
    for ident, before in old.items():
        after = new.get(ident)
        if after is None:
            problems.append(f"{ident}：账本 append-only，禁止删除条目")
            continue
        for field_name in IMMUTABLE:
            if field_name in before and str(before[field_name]) != str(after.get(field_name)):
                problems.append(f"{ident}：{field_name} 不可改（{before[field_name]!r} → {after.get(field_name)!r}）")
        if not _days(before) <= _days(after):
            problems.append(f"{ident}：days 只许并集，不许删天")
        b_status, a_status = before.get("status", "active"), after.get("status", "active")
        if b_status != a_status and (b_status, a_status) not in TRANSITIONS:
            problems.append(f"{ident}：状态只许 pending→active→superseded（{b_status} → {a_status}）")
        if "superseded_by" in before and before["superseded_by"] != after.get("superseded_by"):
            problems.append(f"{ident}：superseded_by 一旦设置不可改")
    return problems


def check_observed(ledger_text: str, observed_dir: Path) -> list[str]:
    if not observed_dir.is_dir():
        return []
    registered = {d.code.value for d in parse_ledger(ledger_text).entries}
    problems = []
    for f in sorted(observed_dir.glob("*.toml")):
        codes = set(tomllib.loads(f.read_text(encoding="utf-8")).get("codes", []))
        unknown = sorted(codes - registered)
        if unknown:
            problems.append(f"{f}：观测到未登记的 code {unknown}")
    return problems


def _baseline(base: str, path: str) -> str | None:
    r = subprocess.run(["git", "show", f"{base}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="ledger/defects.toml")
    ap.add_argument("--base", default="origin/main")
    args = ap.parse_args()
    text = Path(args.path).read_text(encoding="utf-8")
    problems = validate(text)
    if not problems:
        old = _baseline(args.base, args.path)
        if old is None:
            print(f"账本基线 {args.base}:{args.path} 不可得，门禁 fail-closed", file=sys.stderr)
            return 2
        try:
            problems += compare(old, text)
        except (tomllib.TOMLDecodeError, KeyError) as exc:
            print(f"账本基线不可解析，门禁 fail-closed：{exc}", file=sys.stderr)
            return 2
        problems += check_observed(text, Path(args.path).parent / "observed")
    for p in problems:
        print(p)
    print("账本门禁：通过" if not problems else f"账本门禁：{len(problems)} 处违规")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
