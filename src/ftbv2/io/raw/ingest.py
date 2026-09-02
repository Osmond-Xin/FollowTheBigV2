"""摄取（架构图模块表）：一天的 7z → {root}/{stream}/date=YYYYMMDD.parquet + manifest。

只能承诺 preserve 层自洽，不得承诺「逐行无损」。但 V1 的验收缺陷这里全部修掉：
- 行数校验独立计数（表头之后的非空行数，末尾多余换行不算行），与 parquet 行数不符 ⇒ RuntimeError，不写 manifest；
- 表头原文进 manifest（列语义从公理变成数据）；列数以表头字段数为准（含尾逗号产生的幽灵列），有数据行与空 CSV 口径一致；
- 原子写：先写临时文件再 os.replace；一天一把锁（manifest 目录下 .lock，O_EXCL），锁内清扫同一目标残留的 .tmp；
  幂等判据 = manifest 三 stream 齐全且 parquet 哈希核对一致，不是「某个文件存在」；三个 stream 逐个加载即写即释放，不攒内存；
- 输出布局与现有 preserve 逐位兼容：列名 column_1..N 全 large_string + _symbol 列（值 "002783.SZ"），
  行按 _symbol 升序、标的内保持 CSV 原序，row_group_size = schema.ROW_GROUP_ROWS，zstd；
- 7z 用 7zz 一趟流式解出（py7zr 会挂）；解到私有 mkdtemp 目录，用完即删；
- 归档条目校验：拒绝绝对路径、含 ".." 的路径、符号链接（7zz -slt 的属性列 Unix 模式串以 l 开头）、规范化后重复的路径；
  解出的每个文件 resolve 后必须在 scratch 之下且非链接、非硬链接；违反即 RuntimeError 且 root 无任何改动；
  7zz 自身失败也转成 RuntimeError；
- 标的目录名必须匹配 ^\d{6}\.[A-Z]{2,3}$（归档里可有 .BJ 等，形状合法即可；主板与否由前缀决定）；同一 stream 内所有标的的表头必须逐字节一致；
  数据体必须是纯 ASCII（非法字节硬失败，不做 lossy 替换）；0 字节或无表头的 CSV 硬失败；归档里非三类 CSV 的文件硬失败；
- 7zz 调用带超时（列目录 10 分钟、解包 4 小时）；
- 前缀筛选（默认 schema.MAIN_PREFIXES，只存主板）：这是「不得在未声明样本宇宙前删除行」的**显式例外**（Q15，2026-09-02 用户裁定），
  被筛掉的标的按前缀计数进 receipt——丢弃是决策，不是静默；归档里不认识的 CSV 文件名硬失败；
- 幂等绑定来源：manifest 记录 archive_sha256、prefixes 与 7zz 版本；同一天再次摄取时归档哈希或前缀集不同 ⇒ RuntimeError，不静默返回旧 receipt；
- sha256_csv 的输入是规范帧：按标的升序，每个标的贡献三段 symbol、header、body，每段编码为
  `十进制长度的 ASCII 字节 + \\0 + 段字节`（symbol 与 header 用 UTF-8，body 是 CSV 原始字节），段间无其他分隔。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from ftbv2.core.raw.schema import CSV_NAME, MAIN_PREFIXES, ROW_GROUP_ROWS, STREAMS, SYMBOL_COL, Stream, manifest_relpath, parquet_relpath
from ftbv2.core.raw.types import Day, IngestReceipt, Quality, StreamReceipt

_STREAM_OF_CSV = {name: stream for stream, name in CSV_NAME.items()}
_ARCHIVE_SYMBOL_RE = re.compile(r"^\d{6}\.[A-Z]{2,3}$")   # 归档里可能有 .BJ / 基金等，形状合法即可；宇宙由前缀决定
_LIST_TIMEOUT_S = 600
_EXTRACT_TIMEOUT_S = 4 * 3600


def ingest(
    day: Day,
    archive: Path,
    root: Path,
    *,
    prefixes: tuple[str, ...] = MAIN_PREFIXES,
    scratch_parent: Path | None = None,
) -> IngestReceipt:
    """archive 内布局 {YYYYMMDD}/{symbol}/{行情,逐笔委托,逐笔成交}.csv（也接受无日期前缀的扁平布局）。
    CSV：GBK 表头一行 + 纯 ASCII 数据行，所有字段按字符串原样保留（含 '\\x00'）。
    只有表头、零数据行的 CSV 是合法的（全天无委托 / 无成交），照常计数为 0；标的目录里**缺少**某个 stream 的 CSV 文件
    则是残缺归档 ⇒ RuntimeError。
    已完成（manifest 三 stream 齐全且 archive_sha256、prefixes 相同）的天直接返回既有 receipt，不重做。
    scratch_parent：临时解包目录的父目录（默认系统临时目录）；实际解包目录用 mkdtemp 私有创建。"""
    archive = archive.resolve(strict=True)
    root = root.resolve()
    archive_sha = _sha256_file(archive)
    manifest = root / manifest_relpath(day)
    existing = _load_manifest(manifest)
    if existing is not None:
        if existing.archive_sha256 != archive_sha:
            raise RuntimeError(f"{day:%Y-%m-%d} 已摄取过，但归档 sha256 不同")
        if existing.prefixes != tuple(prefixes):
            raise RuntimeError(f"{day:%Y-%m-%d} 已摄取过，但前缀集不同（prefixes）")
        if {s.stream for s in existing.streams} == set(STREAMS):
            for s in existing.streams:
                target = root / parquet_relpath(s.stream, day)
                if not target.exists() or _sha256_file(target) != s.parquet_sha256:
                    raise RuntimeError(f"{day:%Y-%m-%d} 的 {s.stream} parquet 与 manifest 记录的哈希不符，产物被改动过")
            return existing

    _validate_entries(_list_entries(archive))
    lock = root / manifest_relpath(day).replace(".json", ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"{day:%Y-%m-%d} 正被另一个进程摄取（{lock}）") from exc
    os.close(fd)
    scratch = Path(tempfile.mkdtemp(prefix=f"ftbv2-{day:%Y%m%d}-", dir=scratch_parent))
    try:
        _extract(archive, scratch)
        _validate_extracted(scratch)
        csvs, dropped = _discover_csvs(scratch, day, tuple(prefixes))
        receipts: list[StreamReceipt] = []
        for stream in STREAMS:                      # 逐流：加载 → 写盘 → 释放，峰值内存只有一个 stream
            frame, receipt = _load_stream(stream, csvs[stream])
            target = root / parquet_relpath(stream, day)
            _write_stream(target, frame)
            del frame
            receipts.append(StreamReceipt(
                receipt.stream, receipt.n_symbols, receipt.n_rows_csv, receipt.n_rows_parquet, receipt.header,
                target.stat().st_size, _sha256_file(target), receipt.sha256_csv,
            ))
        receipt = IngestReceipt(day, archive, archive_sha, tuple(prefixes), _sevenzip_version(), tuple(receipts), dropped)
        _write_manifest(manifest, receipt)
        return receipt
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        lock.unlink(missing_ok=True)


# ----------------------------------------------------------------- manifest / 哈希


def _load_manifest(path: Path) -> IngestReceipt | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return IngestReceipt(
        dt.date.fromisoformat(data["day"]), Path(data["archive"]), data["archive_sha256"], tuple(data["prefixes"]),
        data["sevenzip_version"], tuple(StreamReceipt(**row) for row in data["streams"]), dict(data["dropped_by_prefix"]),
    )


def _write_manifest(path: Path, receipt: IngestReceipt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "quality": Quality.SELF_CONSISTENT.value, "day": f"{receipt.day:%Y-%m-%d}", "archive": str(receipt.archive),
        "archive_sha256": receipt.archive_sha256, "prefixes": list(receipt.prefixes),
        "sevenzip_version": receipt.sevenzip_version, "streams": [asdict(s) for s in receipt.streams],
        "dropped_by_prefix": receipt.dropped_by_prefix,
    }
    _atomic_write(path, lambda tmp: tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_frame(symbol: str, header: str, body: bytes) -> bytes:
    """sha256_csv 的规范帧（单源，外部校验者按此复算）：三段各为 十进制长度 ASCII + \0 + 段字节。"""
    out = bytearray()
    for payload in (symbol.encode("utf-8"), header.encode("utf-8"), body):
        out += str(len(payload)).encode("ascii") + b"\0" + payload
    return bytes(out)


# ----------------------------------------------------------------- 7z


def _run_7zz(*args: str, timeout: int = _LIST_TIMEOUT_S) -> str:
    try:
        return subprocess.run(["7zz", *args], capture_output=True, text=True, check=True, timeout=timeout).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"7zz 失败：{args[:2]}") from exc


def _sevenzip_version() -> str:
    lines = [ln for ln in _run_7zz("i").splitlines() if ln.strip()]
    return lines[0].strip() if lines else "unknown"


def _list_entries(archive: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in _run_7zz("l", "-slt", "-ba", str(archive)).splitlines():
        if not line.strip():
            if current:
                entries.append(current)
            current = {}
            continue
        key, sep, value = line.partition(" = ")
        if sep:
            current[key] = value
    if current:
        entries.append(current)
    return entries


def _validate_entries(entries: list[dict[str, str]]) -> None:
    seen: set[str] = set()
    for entry in entries:
        text = entry.get("Path")
        if text is None:
            raise RuntimeError("归档条目没有 Path 字段")
        path = Path(text)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"归档条目路径不安全：{text}")
        normalized = path.as_posix()
        if normalized in seen:
            raise RuntimeError(f"归档条目路径重复：{text}")
        seen.add(normalized)
        mode = entry.get("Attributes", "").split()[-1] if entry.get("Attributes") else ""
        if mode[:1] in ("l", "L"):
            raise RuntimeError(f"归档含链接条目：{text}")


def _extract(archive: Path, scratch: Path) -> None:
    _run_7zz("x", "-y", "-bso0", "-bsp0", f"-o{scratch}", str(archive), timeout=_EXTRACT_TIMEOUT_S)


def _validate_extracted(scratch: Path) -> None:
    base = scratch.resolve(strict=True)
    for path in scratch.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"解出的链接被拒绝：{path}")
        if not path.resolve(strict=True).is_relative_to(base):
            raise RuntimeError(f"解出的路径逃逸了 scratch：{path}")
        if path.is_file() and path.stat().st_nlink > 1:
            raise RuntimeError(f"解出的硬链接被拒绝：{path}")


# ----------------------------------------------------------------- 发现与读取 CSV


def _discover_csvs(scratch: Path, day: Day, prefixes: tuple[str, ...]) -> tuple[dict[Stream, list[tuple[str, Path]]], dict[str, int]]:
    day_text = f"{day:%Y%m%d}"
    by_symbol: dict[str, dict[Stream, Path]] = {}
    dropped: dict[str, int] = {}
    dropped_symbols: set[str] = set()
    for path in sorted(p for p in scratch.rglob("*") if p.is_file()):
        parts = path.relative_to(scratch).parts
        if len(parts) == 3 and parts[0] == day_text:
            symbol, name = parts[1], parts[2]
        elif len(parts) == 2:
            symbol, name = parts
        else:
            raise RuntimeError(f"归档里有未登记形状的路径：{path.relative_to(scratch)}")
        stream = _STREAM_OF_CSV.get(name)
        if stream is None:
            raise RuntimeError(f"归档里有未登记的文件：{path.relative_to(scratch)}")
        if not _ARCHIVE_SYMBOL_RE.match(symbol):
            raise RuntimeError(f"归档里的标的目录名不合法：{symbol}")
        if not symbol.startswith(prefixes):
            if symbol not in dropped_symbols:
                dropped_symbols.add(symbol)
                dropped[symbol[:3]] = dropped.get(symbol[:3], 0) + 1
            continue
        by_symbol.setdefault(symbol, {})[stream] = path
    by_stream: dict[Stream, list[tuple[str, Path]]] = {stream: [] for stream in STREAMS}
    for symbol in sorted(by_symbol):
        streams = by_symbol[symbol]
        if set(streams) != set(STREAMS):
            raise RuntimeError(f"标的 {symbol} 缺 stream CSV：{sorted(set(STREAMS) - set(streams))}")
        for stream, path in streams.items():
            by_stream[stream].append((symbol, path))
    for stream, rows in by_stream.items():
        if not rows:
            raise RuntimeError(f"归档里没有任何 {stream} 的 CSV")
    return by_stream, dropped


def _load_stream(stream: Stream, csvs: list[tuple[str, Path]]) -> tuple[pl.DataFrame, StreamReceipt]:
    frames: list[pl.DataFrame] = []
    digest = hashlib.sha256()
    header_text = ""
    n_rows_csv = 0
    for symbol, path in csvs:                      # 已按标的升序
        data = path.read_bytes()                   # 只读一次盘：表头、计数、哈希、解析都从这份字节来
        newline = data.find(b"\n")
        if not data or newline < 0 and not data.strip():
            raise RuntimeError(f"CSV 为空或没有表头：{path}")
        header_bytes = (data if newline < 0 else data[:newline]).rstrip(b"\r")
        body = b"" if newline < 0 else data[newline + 1:]
        if not body.isascii():
            raise RuntimeError(f"CSV 数据体含非 ASCII 字节，不做 lossy 替换：{path}")
        if b'"' in body:                           # 语料没有引号字段；有引号则按行计数与解析器的逻辑行会分叉，硬失败
            raise RuntimeError(f"CSV 数据体含引号，不是登记的形状：{path}")
        header = header_bytes.decode("gbk")
        if header_text and header != header_text:
            raise RuntimeError(f"{stream} 内表头不一致：{symbol} 与首个标的不同")
        header_text = header_text or header
        n_rows = sum(1 for line in body.splitlines() if line)
        n_rows_csv += n_rows
        digest.update(canonical_frame(symbol, header, body))
        frame = _parse_body(body, symbol, len(header.split(",")), n_rows)
        if frame.height != n_rows:
            raise RuntimeError(f"CSV 行数不符：{path}（独立计数 {n_rows}，解析得 {frame.height}）")
        frames.append(frame)
    combined = pl.concat(frames, how="vertical")
    return combined, StreamReceipt(stream, len(csvs), n_rows_csv, combined.height, header_text, 0, "", digest.hexdigest())


def _parse_body(body: bytes, symbol: str, ncols: int, n_rows: int) -> pl.DataFrame:
    columns = [f"column_{i}" for i in range(1, ncols + 1)]
    if n_rows == 0:
        frame = pl.DataFrame({c: pl.Series(c, [], dtype=pl.String) for c in columns})
    else:
        frame = pl.read_csv(
            io.BytesIO(body), has_header=False, encoding="utf8",
            infer_schema_length=0, empty_string_is_null=False, n_rows=n_rows,
        )
        if frame.width != ncols:
            raise RuntimeError(f"{symbol} 的数据行列数 {frame.width} 与表头字段数 {ncols} 不符")
        frame.columns = columns
    return frame.with_columns(pl.lit(symbol).alias(SYMBOL_COL))


# ----------------------------------------------------------------- 写盘


def _write_stream(target: Path, frame: pl.DataFrame) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    schema = pa.schema([(c, pa.large_string()) for c in frame.columns])
    table = frame.to_arrow().cast(schema)          # 零拷贝转 arrow，只做 dtype cast，不经 Python 对象
    _atomic_write(target, lambda tmp: pq.write_table(table, tmp, row_group_size=ROW_GROUP_ROWS, compression="zstd"))


def _atomic_write(target: Path, write) -> None:
    """持有天级锁的前提下：清扫同一目标残留的 .tmp（上次崩溃遗留），写唯一命名的临时文件，失败即删，成功 os.replace。"""
    for stale in target.parent.glob(target.name + "*.tmp"):
        stale.unlink()
    tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        write(tmp)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
