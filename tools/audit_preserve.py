"""原始层审计（薄壳）：compare（两份 preserve 逐 row group 比对）或 mismatch（跨流标的集合差）→ JSON + 收据。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ftbv2.io.raw.audit import compare_preserve, preserve_days, symbol_mismatches
from ftbv2.io.receipt import write_receipt


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
        payload = {"day": args.day.isoformat(), "identical_modulo_null": all(r.identical_modulo_null for r in result),
                   "streams": [asdict(r) for r in result]}
        inputs = {"a": str(args.a), "b": str(args.b)}
    else:
        days = tuple(args.days) if args.days else preserve_days(args.root)
        result = symbol_mismatches(args.root, days)
        payload = {"n_days": len(days), "mismatches": [{"day": r.day.isoformat(), "only_in": r.only_in} for r in result]}
        inputs = {"root": str(args.root), "days": f"{days[0]}..{days[-1]}" if days else ""}
    receipt_id, _ = write_receipt(f"audit_preserve.{args.cmd}", Path(__file__), inputs, {}, payload)
    print(json.dumps({"receipt_id": receipt_id, **payload}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
