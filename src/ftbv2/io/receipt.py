"""收据（工程管束 R1）：任何进入账本 / 数据表 / design-log 的数字必须带收据——命令、工具与源码树哈希、依赖锁哈希、
输入内容哈希、输出哈希、时间。内容寻址 JSON，落在 `.lineage/receipts/<sha256>.json`（git 跟踪，写一次不改）。
绑定范围按词汇表「证据指纹」：源码树（HEAD tree + 是否脏）+ 依赖 lock + 输入摘要 + 命令规格（规范化参数，不用原始 argv）。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path

__all__ = ["sha256_file", "sha256_files", "source_state", "write_receipt"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_files(paths: list[Path]) -> str:
    """一组文件的 Merkle 摘要：按相对名排序，逐个 (名, 内容哈希) 串起来再哈希。"""
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode("utf-8") + b"\0" + sha256_file(p).encode("ascii") + b"\n")
    return h.hexdigest()


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def source_state(tool: Path) -> dict[str, str]:
    """源码树状态（按工具所在仓库定位，不依赖 cwd）：HEAD 的 tree 哈希 + 工作区是否脏 + uv.lock 哈希。任一不可得即报错。"""
    top = _git(tool.resolve().parent, "rev-parse", "--show-toplevel")
    tree = _git(tool.resolve().parent, "rev-parse", "HEAD^{tree}")
    if not top or not tree:
        raise RuntimeError("收据必须由 git 仓库内的工具写：拿不到仓库根或 HEAD tree")
    diff = _git(Path(top), "diff", "--binary", "HEAD")
    lock = Path(top) / "uv.lock"
    if not lock.is_file():
        raise RuntimeError("收据必须绑定 uv.lock，找不到")
    return {"source_tree": tree, "source_dirty": str(bool(diff)).lower(),
            "source_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest() if diff else "",   # 脏树时绑定实际执行的改动
            "uv_lock_sha256": sha256_file(lock)}


def write_receipt(kind: str, tool: Path, args: dict[str, object], inputs: dict[str, str], outputs: dict[str, str],
                  summary: dict, receipts_dir: Path = Path(".lineage/receipts")) -> tuple[str, Path]:
    """args：规范化后的参数（解析后的键值，不是 argv）；inputs / outputs：名字 → 内容哈希或不可变标识（路径只能作定位，不作身份）。
    返回 (receipt_id, 路径)。同内容 ⇒ 同 id；文件已存在则必须与本次内容完全一致，否则硬失败。"""
    body = {
        "kind": kind, "tool": tool.name, "tool_sha256": sha256_file(tool), **source_state(tool),
        "args": {k: str(v) for k, v in sorted(args.items())}, "inputs": inputs, "outputs": outputs, "summary": summary,
    }
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    receipt_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    receipts_dir.mkdir(parents=True, exist_ok=True)
    path = receipts_dir / f"{receipt_id}.json"
    record = {**body, "receipt_id": receipt_id, "written_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if {k: v for k, v in existing.items() if k not in ("receipt_id", "written_at")} != json.loads(canonical):
            raise RuntimeError(f"收据 {receipt_id} 已存在但内容不同：文件被改写过")
        return receipt_id, path
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True, default=str), encoding="utf-8")
    return receipt_id, path
