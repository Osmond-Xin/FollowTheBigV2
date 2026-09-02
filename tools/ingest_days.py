"""批量摄取（薄壳）：归档路径 → ingest_days → JSON 报告 + 收据。任何 skipped / failed / stopped ⇒ 非零退出。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ftbv2.io.raw.ingest import ingest_days
from ftbv2.io.receipt import write_receipt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("archives", nargs="+", type=Path, help="YYYYMMDD.7z 路径（非规范文件名会被登记并导致非零退出）")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--scratch", type=Path, required=True, help="解包临时目录的父目录（本机 SSD）")
    ap.add_argument("--min-free-gb", type=float, default=40.0)
    ap.add_argument("--min-free-pct", type=float, default=5.0)
    ap.add_argument("--continue-on-error", action="store_true")
    args = ap.parse_args()
    result = ingest_days(args.archives, args.root, scratch_parent=args.scratch, min_free_bytes=int(args.min_free_gb * 1e9),
                         min_free_pct=args.min_free_pct, stop_on_error=not args.continue_on_error)
    outcomes = [{**asdict(o), "archive": str(o.archive), "day": o.day.isoformat(),
                 "receipt": None if o.receipt is None else {"streams": [asdict(s) for s in o.receipt.streams],
                                                            "quote_only": list(o.receipt.quote_only_symbols),
                                                            "empty_files": [list(p) for p in o.receipt.empty_files]}}
                for o in result.outcomes]
    receipt_id, _ = write_receipt(
        "ingest_days", Path(__file__), vars(args),
        {o.archive.name: o.receipt.archive_sha256 for o in result.outcomes if o.receipt},
        {o.day.isoformat(): ",".join(s.parquet_sha256 for s in o.receipt.streams) for o in result.outcomes if o.receipt},
        {"ok": result.ok, "n_ok": sum(o.status == "ok" for o in result.outcomes), "skipped": [str(p) for p, _ in result.skipped]},
    )
    print(json.dumps({"receipt_id": receipt_id, "ok": result.ok, "skipped": [[str(p), why] for p, why in result.skipped],
                      "outcomes": outcomes}, ensure_ascii=False, indent=1, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
