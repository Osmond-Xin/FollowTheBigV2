"""读取耗时基准（薄壳）：RawStore 全字段读逐天逐流计时 → JSON + 收据；外推 1122 天。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ftbv2.core.raw.ledger import parse_ledger
from ftbv2.io.raw.audit import preserve_days, read_floor
from ftbv2.io.raw.store import RawStore
from ftbv2.io.receipt import write_receipt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--ledger", type=Path, default=Path("ledger/defects.toml"))
    ap.add_argument("--days", type=dt.date.fromisoformat, nargs="*")
    ap.add_argument("--extrapolate-days", type=int, default=1122)
    args = ap.parse_args()
    ledger = parse_ledger(args.ledger.read_text(encoding="utf-8"))
    days = tuple(args.days) if args.days else preserve_days(args.root)
    timings = read_floor(RawStore(args.root, ledger), ledger, days)
    per_stream = {s: sum(t.seconds for t in timings if t.stream == s) / max(len(days), 1) for s in ("orders", "trades", "xinqing")}
    payload = {"n_days": len(days), "seconds_per_day": per_stream,
               "extrapolated_hours": round(sum(per_stream.values()) * args.extrapolate_days / 3600, 1), "timings": [asdict(t) for t in timings]}
    receipt_id, _ = write_receipt("bench_read", Path(__file__), vars(args), {"root": str(args.root), "ledger": ledger.sha256}, {}, payload)
    print(json.dumps({"receipt_id": receipt_id, **payload}, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
