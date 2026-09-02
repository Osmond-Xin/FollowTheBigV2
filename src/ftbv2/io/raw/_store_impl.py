from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

import polars as pl
import pyarrow.parquet as pq

from ftbv2.core.raw.decode import in_windows, to_int64, to_time_ms
from ftbv2.core.raw.ledger import DefectLedger
from ftbv2.core.raw.schema import STREAMS, SYMBOL_COL, Stream, field
from ftbv2.core.raw.types import (
    Catalog,
    Day,
    FileMeta,
    FilePlan,
    Gap,
    GapReason,
    Quality,
    ReadResult,
    ReadStats,
    RowGroupMeta,
    ScanPlan,
)

if TYPE_CHECKING:
    from ftbv2.io.raw.store import RawStore


class State:
    def __init__(self, root: Path, ledger: DefectLedger) -> None:
        self.root = root
        self.ledger = ledger


_STATE: WeakKeyDictionary[RawStore, State] = WeakKeyDictionary()


def init(store: RawStore, root: Path, ledger: DefectLedger) -> None:
    try:
        resolved = root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"not-mounted: {root}") from exc
    for stream in STREAMS:
        if not (resolved / stream).is_dir():
            raise RuntimeError(f"raw stream dir missing: {stream} under {resolved}")
    _STATE[store] = State(resolved, ledger)


def catalog(store: RawStore, stream: Stream, days: tuple[Day, ...]) -> Catalog:
    state = _state(store)
    files = []
    missing = []
    for day in days:
        path = _parquet_path(state.root, stream, day)
        if not path.exists():
            missing.append(day)
            continue
        meta = pq.read_metadata(path)
        columns = tuple(meta.schema.names)
        symbol_index = columns.index(SYMBOL_COL)
        row_groups = tuple(_row_group_meta(meta, index, symbol_index) for index in range(meta.num_row_groups))
        files.append(FileMeta(path, stream, day, meta.num_rows, columns, row_groups))
    return Catalog(stream, tuple(files), tuple(missing))


def execute(store: RawStore, scan_plan: ScanPlan) -> ReadResult:
    frames = []
    present_by_day: dict[Day, set[str]] = {}
    for file_plan in scan_plan.files:
        raw = _read_file_plan(file_plan)
        if "column_4" in raw.columns and "time_6digit" not in file_plan.patches:
            _fail_on_unregistered_short_time(raw, scan_plan.request.stream, file_plan.day)
        if SYMBOL_COL in raw.columns:
            present_by_day[file_plan.day] = set(raw[SYMBOL_COL].drop_nulls().to_list())
        semantic = _decode_file(raw, file_plan, scan_plan)
        frames.append(semantic.select(scan_plan.output_fields))

    frame = pl.concat(frames, how="vertical") if frames else _empty_output(scan_plan)
    gaps = _gaps(store, scan_plan, present_by_day)
    row_groups_total = sum(file_plan.total_row_groups for file_plan in scan_plan.files)
    row_groups_read = sum(len(file_plan.row_groups) for file_plan in scan_plan.files)
    bytes_total = sum(file_plan.total_bytes for file_plan in scan_plan.files)
    bytes_read = sum(sum(rg.byte_size for rg in file_plan.row_groups) for file_plan in scan_plan.files)
    stats = ReadStats(row_groups_total, row_groups_read, bytes_total, bytes_read, frame.height)
    return ReadResult(frame, gaps, stats)


def inspect_raw(
    store: RawStore,
    stream: Stream,
    day: Day,
    columns: tuple[str, ...],
    symbols: frozenset[str] | None = None,
) -> pl.DataFrame:
    state = _state(store)
    projection = list(columns)
    if symbols is not None and SYMBOL_COL not in projection:
        projection.append(SYMBOL_COL)
    table = pq.read_table(_parquet_path(state.root, stream, day), columns=projection, pre_buffer=True)
    frame = pl.from_arrow(table)
    if symbols is not None:
        frame = frame.filter(pl.col(SYMBOL_COL).is_in(sorted(symbols)))
    return frame.select(list(columns))


def days(store: RawStore, quality_filter: Quality | None = None) -> tuple[Day, ...]:
    state = _state(store)
    by_stream = [_stream_days(state.root / stream) for stream in STREAMS]
    common = set.intersection(*by_stream) if by_stream else set()
    ordered = tuple(sorted(common))
    if quality_filter is None:
        return ordered
    return tuple(day for day in ordered if quality(store, day) is quality_filter)


def quality(store: RawStore, day: Day) -> Quality:
    path = _state(store).root / "manifest" / f"{day:%Y%m%d}.json"
    if not path.exists():
        return Quality.UNVERIFIED
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"corrupt manifest: {path}") from exc
    value = data.get("quality", Quality.SELF_CONSISTENT.value)
    try:
        return Quality(value)
    except ValueError as exc:
        raise RuntimeError(f"unknown quality {value!r} in {path}") from exc


def _parquet_path(root: Path, stream: Stream, day: Day) -> Path:
    return root / stream / f"date={day:%Y%m%d}.parquet"


def _state(store: RawStore) -> State:
    return _STATE[store]


def _row_group_meta(meta: pq.FileMetaData, index: int, symbol_index: int) -> RowGroupMeta:
    group = meta.row_group(index)
    stats = group.column(symbol_index).statistics
    byte_size = sum(group.column(i).total_compressed_size for i in range(group.num_columns))
    return RowGroupMeta(index, group.num_rows, byte_size, stats.min, stats.max)


def _read_file_plan(file_plan: FilePlan) -> pl.DataFrame:
    if not file_plan.row_groups:
        return pl.DataFrame({column: pl.Series(column, [], dtype=pl.String) for column in file_plan.columns})
    parquet_file = pq.ParquetFile(file_plan.path, pre_buffer=True)
    table = parquet_file.read_row_groups(
        [row_group.index for row_group in file_plan.row_groups],
        columns=list(file_plan.columns),
    )
    return pl.from_arrow(table)


def _fail_on_unregistered_short_time(frame: pl.DataFrame, stream: Stream, day: Day) -> None:
    has_short = frame.select(
        (pl.col("column_4").str.strip_chars().str.len_chars() <= 7).fill_null(False).any()
    ).item()
    if has_short:
        raise RuntimeError(f"time_6digit not registered for {day:%Y-%m-%d} {stream}")


def _decode_file(raw: pl.DataFrame, file_plan: FilePlan, scan_plan: ScanPlan) -> pl.DataFrame:
    needed = list(scan_plan.output_fields)
    if scan_plan.request.windows is not None and "time_ms" not in needed:
        needed.append("time_ms")
    expressions = [pl.lit(file_plan.day).cast(pl.Date).alias("day")]
    for name in needed:
        if name == "day":
            continue
        if name == "symbol":
            expressions.append(pl.col(SYMBOL_COL).alias("symbol"))
            continue
        schema_field = field(scan_plan.request.stream, name)
        expressions.append(_decode_field(schema_field, "time_6digit" in file_plan.patches).alias(name))
    frame = raw.with_columns(expressions)
    if scan_plan.request.symbols is not None:
        frame = frame.filter(pl.col("symbol").is_in(sorted(scan_plan.request.symbols)))
    if scan_plan.request.windows is not None:
        frame = frame.filter(in_windows("time_ms", scan_plan.request.windows))
    return frame


def _decode_field(schema_field, allow_6digit: bool) -> pl.Expr:
    if schema_field.kind == "time":
        return to_time_ms(schema_field.column, allow_6digit=allow_6digit)
    if schema_field.kind in {"int", "price"}:
        return to_int64(schema_field.column)
    return pl.col(schema_field.column)


def _gaps(store: RawStore, scan_plan: ScanPlan, present_by_day: dict[Day, set[str]]) -> tuple[Gap, ...]:
    file_days = {file_plan.day for file_plan in scan_plan.files}
    gaps = []
    for day in scan_plan.request.days:
        defects = _defects(store, day, scan_plan.request.stream)
        if day not in file_days:
            gaps.append(Gap(day, scan_plan.request.stream, GapReason.DAY_MISSING, None, defects))
            continue
        if scan_plan.request.symbols is None:
            continue
        present = present_by_day.get(day, set())
        for symbol in sorted(scan_plan.request.symbols):
            if symbol not in present:
                gaps.append(Gap(day, scan_plan.request.stream, GapReason.SYMBOL_ABSENT, symbol, defects))
    return tuple(gaps)


def _defects(store: RawStore, day: Day, stream: Stream) -> tuple[str, ...]:
    return tuple(defect.code.value for defect in _state(store).ledger.for_day(day, stream))


def _empty_output(scan_plan: ScanPlan) -> pl.DataFrame:
    schema = {}
    for name in scan_plan.output_fields:
        if name == "day":
            schema[name] = pl.Date
        elif name == "symbol":
            schema[name] = pl.String
        else:
            kind = field(scan_plan.request.stream, name).kind
            schema[name] = pl.Int64 if kind in {"time", "int", "price"} else pl.String
    return pl.DataFrame({name: pl.Series(name, [], dtype=dtype) for name, dtype in schema.items()})


def _stream_days(path: Path) -> set[dt.date]:
    out = set()
    for parquet in path.glob("date=*.parquet"):
        try:
            out.add(dt.datetime.strptime(parquet.stem, "date=%Y%m%d").date())
        except ValueError:
            continue
    return out
