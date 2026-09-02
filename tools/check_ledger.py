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
import datetime as dt
import subprocess
import sys
import tomllib
from pathlib import Path

from ftbv2.core.raw.ledger import LIVE, DefectCode, parse_ledger

IMMUTABLE = ("code", "kind", "stream", "created_at", "evidence", "decision_ref")
TRANSITIONS = {("pending", "active"), ("active", "superseded"), ("pending", "rejected")}


def validate(text: str) -> list[str]:
    """结构 + 枚举相等。parse_ledger 抛的 ValueError 也算一条违规。"""
    try:
        ledger = parse_ledger(text)
    except (ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        return [f"账本解析失败：{exc}"]
    live = {d.code for d in ledger.entries if d.status in LIVE}
    enum = {c.value for c in DefectCode}
    problems = []
    if live != enum:
        problems.append(f"代码枚举必须等于账本 active ∪ pending 的 code 集合：只在代码 {sorted(enum - live)}，只在账本 {sorted(live - enum)}")
    return problems


def _rows(text: str) -> dict[str, dict]:
    """基线与当前都按原始 TOML 字典比较：基线可能是旧结构（缺新字段），不能用严格解析器读它。"""
    rows = tomllib.loads(text).get("defect", [])
    return {str(r["id"]): r for r in rows if "id" in r}


def _norm(value: object) -> object:
    """日期按 ISO 日期字符串比较；datetime 不归一化成 date（严格解析会先把它拒掉）。"""
    return value.isoformat() if type(value) is dt.date else value


def _days(row: dict) -> set[object]:
    return {_norm(d) for d in row.get("days", [])}


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
            if field_name in before and _norm(before[field_name]) != _norm(after.get(field_name)):
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
    registered = {d.code for d in parse_ledger(ledger_text).entries if d.status == "active"}   # 只有 active 算已登记
    problems = []
    for f in sorted(observed_dir.glob("*.toml")):
        try:
            codes = set(tomllib.loads(f.read_text(encoding="utf-8")).get("codes", []))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            problems.append(f"{f}：观测文件不可解析（{exc}），fail-closed")
            continue
        unknown = sorted(codes - registered)
        if unknown:
            problems.append(f"{f}：观测到未登记（非 active）的 code {unknown}")
    return problems


def _baseline(path: str, unsafe_base: str | None) -> tuple[str, str] | None:
    """基线 = `merge-base HEAD origin/main` 那一版账本（受保护 main 的祖先），不接受环境变量覆盖；
    --unsafe-base 只给本地调试，gate.sh 不透传。返回 (oid, 文本)。"""
    if unsafe_base is None:
        mb = subprocess.run(["git", "merge-base", "HEAD", "origin/main"], capture_output=True, text=True)
        if mb.returncode != 0:
            return None
        oid = mb.stdout.strip()
    else:
        oid = unsafe_base
    r = subprocess.run(["git", "show", f"{oid}:{path}"], capture_output=True, text=True)
    return (oid, r.stdout) if r.returncode == 0 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="ledger/defects.toml")
    ap.add_argument("--unsafe-base", default=None, help="仅本地调试：覆盖基线 ref。CI / gate.sh 不得使用")
    args = ap.parse_args()
    text = Path(args.path).read_text(encoding="utf-8")
    problems = validate(text)
    if not problems:
        found = _baseline(args.path, args.unsafe_base)
        if found is None:
            print(f"账本基线（merge-base HEAD origin/main）:{args.path} 不可得，门禁 fail-closed", file=sys.stderr)
            return 2
        oid, old = found
        print(f"账本基线：{oid[:12]}" + ("（--unsafe-base，仅调试）" if args.unsafe_base else "（merge-base HEAD origin/main）"))
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
