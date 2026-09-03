"""假墙密度回归（薄壳）：RawStore 读样本日 → ftbv2.io.events.probe_walls → JSON + 收据 + 准入判定。

「一堵墙必须到过最优价吗」由数据回归回答，不由我们定义（2026-09-03 用户裁定）。

**实测的数字落在收据里，不写回源码**（红队 2026-09-03 架构严重 5）：本工具跑完后
自己调 `admit_full_extraction()`，把「这批实测够不够格进全量」当场判出来并打印。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from ftbv2.core.raw import manifest_relpath, parse_ledger
from ftbv2.core.registry import TOTAL_ORDER, EvidenceRef, admit_full_extraction, digest, spec
from ftbv2.io.events import KIND, probe_walls
from ftbv2.io.raw import RawStore
from ftbv2.io.receipt import sha256_files, write_receipt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--day", type=dt.date.fromisoformat, action="append", required=True,
                    help="样本日，可重复。协议要求 ≥ 5 天，覆盖不同行情")
    ap.add_argument("--ledger", type=Path, default=Path("ledger/defects.toml"))
    ap.add_argument("--sample", type=int, default=0, help="每天随机抽样标的数；0 = 全部")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    days = tuple(args.day)
    ledger = parse_ledger(args.ledger.read_text(encoding="utf-8"))
    probe = probe_walls(RawStore(args.root, ledger), ledger, days, args.sample, args.seed)

    payload = asdict(probe)
    manifests = [args.root / manifest_relpath(d) for d in days]
    receipt_id, _ = write_receipt(
        "probe_walls", Path(__file__), vars(args),
        {"root": str(args.root), "ledger": ledger.sha256, "days": ",".join(d.isoformat() for d in days),
         "input_manifest_sha256": sha256_files(manifests), "spec_digest": digest()},
        {}, payload)
    evidence = EvidenceRef(
        receipt_id=receipt_id,
        input_manifest_sha256=sha256_files(manifests),
        extractor_commit=_head_commit(),
        spec_digest=digest(),
        sort_key=TOTAL_ORDER,
    )
    verdict = _admit(probe.measurement(evidence))
    print(json.dumps({"receipt_id": receipt_id, "准入": verdict,
                      "目标": asdict(spec(KIND).density_target), **payload},
                     ensure_ascii=False, indent=1, default=str))
    return 0


def _admit(measurement: object) -> str:
    """当场判准入。**不过不是异常路径**——它就是本工具要回答的问题，答案要打印出来给人看。"""
    try:
        admit_full_extraction(KIND, measurement)          # type: ignore[arg-type]
    except ValueError as e:
        return f"拒绝：{e}"
    return "通过：可下发全量提取"


def _head_commit() -> str:
    r = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("拿不到 HEAD commit：实测必须说得出提取器是哪一版")
    return r.stdout.strip()


if __name__ == "__main__":
    sys.exit(main())
