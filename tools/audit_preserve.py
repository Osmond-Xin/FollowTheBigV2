"""原始层审计（薄壳）：compare（两份 preserve 逐 row group 比对）或 mismatch（跨流标的集合差）→ JSON + 收据。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ftbv2.core.raw.schema import STREAMS, parquet_relpath
from ftbv2.io.raw.audit import compare_preserve, preserve_days, symbol_mismatches
from ftbv2.io.receipt import sha256_files, write_receipt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare"); c.add_argument("--a", type=Path, required=True); c.add_argument("--b", type=Path, required=True)
    c.add_argument("--day", type=dt.date.fromisoformat, required=True)
    m = sub.add_parser("mismatch"); m.add_argument("--root", type=Path, required=True)
    m.add_argument("--days", type=dt.date.fromisoformat, nargs="*", help="省略 = root 下全部天")
    args = ap.parse_args()
    if args.cmd == "compare":
        result = compare_preserve(args.a, args.b, args.day)
        ok = all(r.identical_modulo_null for r in result)
        payload = {"day": args.day.isoformat(), "identical_modulo_null": ok, "streams": [asdict(r) for r in result]}
        files = [args.a / parquet_relpath(s, args.day) for s in STREAMS] + [args.b / parquet_relpath(s, args.day) for s in STREAMS]
        inputs = {"a_files": sha256_files(files[:3]), "b_files": sha256_files(files[3:])}
    else:
        days = tuple(args.days) if args.days else preserve_days(args.root)
        result = symbol_mismatches(args.root, days)
        ok = True
        payload = {"n_days": len(days), "mismatches": [{"day": r.day.isoformat(), "only_in": r.only_in} for r in result]}
        inputs = {"symbol_columns": sha256_files([args.root / parquet_relpath(s, d) for d in days for s in STREAMS])}
    receipt_id, _ = write_receipt(f"audit_preserve.{args.cmd}", Path(__file__), vars(args), inputs, {}, payload)
    print(json.dumps({"receipt_id": receipt_id, **payload}, ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
