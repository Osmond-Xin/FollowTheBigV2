"""假墙密度回归（薄壳）：RawStore 读一天 → ftbv2.io.events.probe_walls → JSON + 收据。

「一堵墙必须到过最优价吗」由数据回归回答，不由我们定义（2026-09-03 用户裁定）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ftbv2.core.raw import parse_ledger
from ftbv2.io.events import probe_walls
from ftbv2.io.raw import RawStore
from ftbv2.io.receipt import write_receipt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--day", type=dt.date.fromisoformat, required=True)
    ap.add_argument("--ledger", type=Path, default=Path("ledger/defects.toml"))
    ap.add_argument("--sample", type=int, default=0, help="随机抽样标的数；0 = 全部")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    ledger = parse_ledger(args.ledger.read_text(encoding="utf-8"))
    probe = probe_walls(RawStore(args.root, ledger), ledger, args.day, args.sample, args.seed)
    payload = asdict(probe)
    receipt_id, _ = write_receipt(
        "probe_walls", Path(__file__), vars(args),
        {"root": str(args.root), "ledger": ledger.sha256, "day": args.day.isoformat()}, {}, payload)
    print(json.dumps({"receipt_id": receipt_id, **payload}, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
