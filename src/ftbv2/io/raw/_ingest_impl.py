from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from ftbv2.core.raw.schema import CSV_NAME, ROW_GROUP_ROWS, STREAMS, SYMBOL_COL
from ftbv2.core.raw.types import Day, IngestReceipt, Quality, StreamReceipt


def ingest_day(
    day: Day,
    archive: Path,
    root: Path,
    prefixes: tuple[str, ...],
    scratch_parent: Path | None,
) -> IngestReceipt:
    archive = archive.resolve(strict=True)
    root = root.resolve()
    archive_sha = _sha256_file(archive)
    manifest = root / "manifest" / f"{day:%Y%m%d}.json"
    existing = _load_existing(manifest)
    if existing is not None:
        if existing.archive_sha256 != archive_sha:
            raise RuntimeError(f"同一天归档 sha256 不同：{day:%Y-%m-%d}")
        if existing.prefixes != prefixes:
            raise RuntimeError(f"同一天摄取前缀不同：{day:%Y-%m-%d}")
        if {stream.stream for stream in existing.streams} == set(STREAMS):
            return existing

    sevenzip_version = _sevenzip_version()
    entries = _list_entries(archive)
    _validate_entries(entries)
    scratch = Path(tempfile.mkdtemp(prefix=f"ftbv2-{day:%Y%m%d}-", dir=scratch_parent))
    try:
        _extract_archive(archive, scratch)
        _validate_extracted(scratch)
        csvs, dropped = _discover_csvs(scratch, day, prefixes)
        stream_receipts = []
        stream_frames = {}
        for stream in STREAMS:
            frame, receipt = _stream_frame(stream, csvs[stream])
            stream_frames[stream] = frame
            stream_receipts.append(receipt)
        parquet_bytes = _write_streams(root, day, stream_frames)
        stream_receipts = [
            StreamReceipt(
                receipt.stream,
                receipt.n_symbols,
                receipt.n_rows_csv,
                receipt.n_rows_parquet,
                receipt.header,
                parquet_bytes[receipt.stream],
                receipt.sha256_csv,
            )
            for receipt in stream_receipts
        ]
        receipt = IngestReceipt(
            day,
            archive,
            archive_sha,
            prefixes,
            sevenzip_version,
            tuple(stream_receipts),
            dropped,
        )
        _write_manifest(manifest, receipt)
        return receipt
    finally:
        shutil.rmtree(scratch)


def _load_existing(path: Path) -> IngestReceipt | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    streams = tuple(StreamReceipt(**row) for row in data["streams"])
    return IngestReceipt(
        dt.date.fromisoformat(data["day"]),
        Path(data["archive"]),
        data["archive_sha256"],
        tuple(data["prefixes"]),
        data["sevenzip_version"],
        streams,
        dict(data.get("dropped_by_prefix", {})),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sevenzip_version() -> str:
    result = subprocess.run(["7zz"], capture_output=True, text=True, check=True)
    return result.stdout.splitlines()[1] if len(result.stdout.splitlines()) > 1 else result.stdout.strip()


def _list_entries(archive: Path) -> list[dict[str, str]]:
    result = subprocess.run(
        ["7zz", "l", "-slt", "-ba", str(archive)],
        capture_output=True,
        text=True,
        check=True,
    )
    entries = []
    current = {}
    for line in result.stdout.splitlines():
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
    seen = set()
    for entry in entries:
        path_text = entry.get("Path")
        if path_text is None:
            continue
        path = Path(path_text)
        parts = path.parts
        if path.is_absolute() or ".." in parts:
            raise RuntimeError(f"unsafe archive path: {path_text}")
        normalized = Path(*parts).as_posix()
        if normalized in seen:
            raise RuntimeError(f"duplicate archive path: {path_text}")
        seen.add(normalized)
        attrs = entry.get("Attributes", "")
        if "L" in attrs or "Link" in entry:
            raise RuntimeError(f"archive link entry rejected: {path_text}")


def _extract_archive(archive: Path, scratch: Path) -> None:
    subprocess.run(
        ["7zz", "x", "-y", "-bso0", "-bsp0", f"-o{scratch}", str(archive)],
        check=True,
    )


def _validate_extracted(scratch: Path) -> None:
    root = scratch.resolve(strict=True)
    for path in scratch.rglob("*"):
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise RuntimeError(f"extracted path escaped scratch: {path}")
        if path.is_symlink():
            raise RuntimeError(f"extracted link rejected: {path}")
        if path.is_file() and path.stat().st_nlink > 1:
            raise RuntimeError(f"extracted hard link rejected: {path}")


def _discover_csvs(
    scratch: Path,
    day: Day,
    prefixes: tuple[str, ...],
) -> tuple[dict[str, list[tuple[str, Path]]], dict[str, int]]:
    day_text = f"{day:%Y%m%d}"
    by_symbol: dict[str, dict[str, Path]] = {}
    dropped: dict[str, int] = {}
    dropped_symbols = set()
    for path in sorted(scratch.rglob("*.csv")):
        rel = path.relative_to(scratch)
        parsed = _parse_csv_path(rel, day_text)
        if parsed is None:
            continue
        symbol, name = parsed
        stream = _stream_for_csv(name)
        if stream is None:
            continue
        prefix = symbol[:3]
        if not symbol.startswith(prefixes):
            if symbol not in dropped_symbols:
                dropped[prefix] = dropped.get(prefix, 0) + 1
                dropped_symbols.add(symbol)
            continue
        streams = by_symbol.setdefault(symbol, {})
        if stream in streams:
            raise RuntimeError(f"duplicate csv for {symbol} {stream}")
        streams[stream] = path
    by_stream: dict[str, list[tuple[str, Path]]] = {stream: [] for stream in STREAMS}
    for symbol, streams in by_symbol.items():
        if set(streams) != set(STREAMS):
            raise RuntimeError(f"symbol missing stream csv: {symbol}")
        for stream, path in streams.items():
            by_stream[stream].append((symbol, path))
    for stream, rows in by_stream.items():
        if not rows:
            raise RuntimeError(f"archive missing stream csv: {stream}")
    return {stream: sorted(rows) for stream, rows in by_stream.items()}, dropped


def _parse_csv_path(rel: Path, day_text: str) -> tuple[str, str] | None:
    parts = rel.parts
    if len(parts) == 3 and parts[0] == day_text:
        return parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1]
    return None


def _stream_for_csv(name: str) -> str | None:
    for stream, csv_name in CSV_NAME.items():
        if name == csv_name:
            return stream
    return None


def _stream_frame(stream: str, csvs: list[tuple[str, Path]]) -> tuple[pl.DataFrame, StreamReceipt]:
    frames = []
    digest = hashlib.sha256()
    header_text = ""
    n_rows_csv = 0
    for symbol, path in csvs:
        header, body = _split_csv(path)
        if not header_text:
            header_text = header
        n_rows = _count_body_rows(body)
        n_rows_csv += n_rows
        _hash_csv_part(digest, symbol, header, body)
        frame = _read_csv_body(path, symbol, header, body, n_rows)
        if frame.height != n_rows:
            raise RuntimeError(f"CSV 行数不符：{path}")
        frames.append(frame)
    combined = pl.concat(frames, how="vertical") if frames else pl.DataFrame()
    receipt = StreamReceipt(
        stream,
        len(csvs),
        n_rows_csv,
        combined.height,
        header_text,
        0,
        digest.hexdigest(),
    )
    return combined, receipt


def _split_csv(path: Path) -> tuple[str, bytes]:
    data = path.read_bytes()
    first_newline = data.find(b"\n")
    if first_newline < 0:
        header_bytes = data.rstrip(b"\r")
        body = b""
    else:
        header_bytes = data[:first_newline].rstrip(b"\r")
        body = data[first_newline + 1 :]
    return header_bytes.decode("gbk"), body


def _count_body_rows(body: bytes) -> int:
    return sum(1 for line in body.splitlines() if line)


def _hash_csv_part(digest, symbol: str, header: str, body: bytes) -> None:
    for payload in (symbol.encode("utf-8"), header.encode("utf-8"), body):
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")


def _read_csv_body(path: Path, symbol: str, header: str, body: bytes, n_rows: int) -> pl.DataFrame:
    ncols = _column_count(header, body)
    if n_rows == 0:
        frame = pl.DataFrame({f"column_{index}": pl.Series([], dtype=pl.String) for index in range(1, ncols + 1)})
    else:
        frame = pl.read_csv(
            path,
            has_header=False,
            skip_rows=1,
            n_rows=n_rows,
            encoding="utf8-lossy",
            infer_schema_length=0,
            empty_string_is_null=False,
        )
        frame = frame.rename({old: f"column_{index}" for index, old in enumerate(frame.columns, 1)})
    for index in range(len(frame.columns) + 1, ncols + 1):
        frame = frame.with_columns(pl.lit("").alias(f"column_{index}"))
    return frame.with_columns(pl.lit(symbol).alias(SYMBOL_COL))


def _column_count(header: str, body: bytes) -> int:
    for line in body.splitlines():
        if line:
            return len(line.split(b","))
    return len(header.rstrip(",").split(","))


def _write_streams(root: Path, day: Day, frames: dict[str, pl.DataFrame]) -> dict[str, int]:
    parquet_bytes = {}
    for stream, frame in frames.items():
        target = root / stream / f"date={day:%Y%m%d}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
        _write_parquet(frame, tmp)
        os.replace(tmp, target)
        parquet_bytes[stream] = target.stat().st_size
    return parquet_bytes


def _write_parquet(frame: pl.DataFrame, path: Path) -> None:
    columns = [column for column in frame.columns if column != "_parquet_bytes"]
    arrays = {column: pa.array(frame[column].cast(pl.String).to_list(), pa.large_string()) for column in columns}
    table = pa.table(arrays, schema=pa.schema([(column, pa.large_string()) for column in columns]))
    pq.write_table(table, path, row_group_size=ROW_GROUP_ROWS, compression="zstd")


def _write_manifest(path: Path, receipt: IngestReceipt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "quality": Quality.SELF_CONSISTENT.value,
        "day": f"{receipt.day:%Y-%m-%d}",
        "archive": str(receipt.archive),
        "archive_sha256": receipt.archive_sha256,
        "prefixes": list(receipt.prefixes),
        "sevenzip_version": receipt.sevenzip_version,
        "streams": [asdict(stream) for stream in receipt.streams],
        "dropped_by_prefix": receipt.dropped_by_prefix,
    }
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
