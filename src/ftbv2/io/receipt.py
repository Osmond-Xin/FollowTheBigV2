"""收据（工程管束 R1）：任何进入账本 / 数据表 / design-log 的数字必须带收据——命令、工具哈希、输入哈希、输出哈希、时间。
收据是内容寻址的 JSON，落在 `.lineage/receipts/<sha256 前 16 位>.json`（git 跟踪，写一次不改）。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_receipt(kind: str, tool: Path, inputs: dict[str, str], outputs: dict[str, str], summary: dict,
                  receipts_dir: Path = Path(".lineage/receipts")) -> tuple[str, Path]:
    """inputs / outputs：名字 → sha256 或不可变标识。返回 (receipt_id, 路径)。同内容 ⇒ 同 id，幂等。"""
    body = {
        "kind": kind, "argv": sys.argv, "tool": str(tool), "tool_sha256": sha256_file(tool),
        "inputs": inputs, "outputs": outputs, "summary": summary,
        "written_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    canonical = json.dumps({k: v for k, v in body.items() if k != "written_at"}, ensure_ascii=False, sort_keys=True, default=str)
    receipt_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    body["receipt_id"] = receipt_id
    receipts_dir.mkdir(parents=True, exist_ok=True)
    path = receipts_dir / f"{receipt_id}.json"
    if not path.exists():
        path.write_text(json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True, default=str), encoding="utf-8")
    return receipt_id, path
