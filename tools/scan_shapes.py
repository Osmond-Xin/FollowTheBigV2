"""全语料形状扫描（薄壳）：每天每流时间串长度分布 → TSV（可续跑：已在输出里的 (day, stream) 跳过）+ 收据。"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from ftbv2.io.raw.audit import preserve_days, scan_time_shapes
from ftbv2.io.receipt import sha256_file, write_receipt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--days", type=dt.date.fromisoformat, nargs="*")
    args = ap.parse_args()
    done: set[tuple[str, str]] = set()
    if args.out.exists():
        done = {tuple(line.split("\t")[:2]) for line in args.out.read_text(encoding="utf-8").splitlines()[1:]}
    else:
        args.out.write_text("day\tstream\tlen\tcount\n", encoding="utf-8")
    days = tuple(args.days) if args.days else preserve_days(args.root)
    with args.out.open("a", encoding="utf-8") as f:
        for obs in scan_time_shapes(args.root, [d for d in days if not any((f"{d:%Y%m%d}", s) in done for s in ("orders", "trades", "xinqing"))]):
            for length, count in sorted(obs.lengths.items()):
                f.write(f"{obs.day:%Y%m%d}\t{obs.stream}\t{length}\t{count}\n")
    receipt_id, _ = write_receipt("scan_shapes", Path(__file__), {"root": str(args.root), "days": f"{days[0]}..{days[-1]}" if days else ""},
                                  {str(args.out): sha256_file(args.out)}, {"n_days": len(days)})
    print(f"receipt {receipt_id} → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
