"""原始层审计（IO 层）：与另一份 preserve 逐 row group 比对、全语料形状扫描、跨流标的集合差、读取耗时基准。
判据全在这里；tools/ 里只有 argparse 薄壳。V1 preserve 只作 sanity 对照，byte-exact 判据来自摄取收据的 sha256_csv。"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ftbv2.io.raw.store import RawStore
from ftbv2.core.raw import (
    Day,
    DefectLedger,
    FIELDS,
    ReadRequest,
    STREAMS,
    SYMBOL_COL,
    Stream,
    parquet_relpath,
    plan,
)


@dataclass(frozen=True)
class StreamCompare:
    stream: Stream
    rows: tuple[int, int]
    row_groups: tuple[int, int]
    schema_equal: bool
    null_columns_a: dict[str, int] = field(default_factory=dict)   # A 里为 null 的格数（按列）
    null_columns_b: dict[str, int] = field(default_factory=dict)   # B 里为 null 的格数（按列）
    cells_differ: dict[str, int] = field(default_factory=dict)      # 两侧都把 null 视作 '' 后仍不同的格数（按列）；行数 / schema 不等时不比

    @property
    def identical_modulo_null(self) -> bool:
        """行数、row group 数（preserve 布局是接口不变量）、schema 全等且逐格无差异。null ≡ '' 只是等价类，两侧都规约。"""
        return self.rows[0] == self.rows[1] and self.row_groups[0] == self.row_groups[1] and self.schema_equal and not self.cells_differ


def compare_preserve(root_a: Path, root_b: Path, day: Day, streams: Iterable[Stream] = STREAMS) -> tuple[StreamCompare, ...]:
    """逐 row group、逐列、逐行比对 A（如 V1 preserve）与 B（如 V2 摄取产物）。null ≡ '' 之外的差异按列计数。"""
    out = []
    for stream in streams:
        fa = pq.ParquetFile(root_a / parquet_relpath(stream, day), pre_buffer=True)
        fb = pq.ParquetFile(root_b / parquet_relpath(stream, day), pre_buffer=True)
        ma, mb = fa.metadata, fb.metadata
        names = fb.schema_arrow.names
        schema_equal = fa.schema_arrow.equals(fb.schema_arrow)
        nulls_a: dict[str, int] = {}
        nulls_b: dict[str, int] = {}
        differ: dict[str, int] = {}
        if ma.num_rows == mb.num_rows and ma.num_row_groups == mb.num_row_groups and schema_equal:
            for i in range(ma.num_row_groups):
                a = fa.read_row_group(i, columns=names).cast(fb.schema_arrow)
                b = fb.read_row_group(i, columns=names)
                for c in names:
                    if a[c].null_count:
                        nulls_a[c] = nulls_a.get(c, 0) + a[c].null_count
                    if b[c].null_count:
                        nulls_b[c] = nulls_b.get(c, 0) + b[c].null_count
                    ne = pc.invert(pc.equal(pc.fill_null(a[c], ""), pc.fill_null(b[c], "")))   # 两侧都规约，双 null 相等
                    if k := (pc.sum(ne).as_py() or 0):
                        differ[c] = differ.get(c, 0) + k
        out.append(StreamCompare(stream, (ma.num_rows, mb.num_rows), (ma.num_row_groups, mb.num_row_groups), schema_equal,
                                 nulls_a, nulls_b, differ))
    return tuple(out)


@dataclass(frozen=True)
class ShapeObservation:
    day: Day
    stream: Stream
    lengths: dict[int, int]        # 时间串（去空白）长度 → 行数；-1 = null


def scan_time_shapes(root: Path, days: Iterable[Day], streams: Iterable[Stream] = STREAMS) -> Iterator[ShapeObservation]:
    """全语料时间列形状：每天每流 column_4 的长度分布。只读一列，单流串行。"""
    for day in days:
        for stream in streams:
            path = root / parquet_relpath(stream, day)
            col = pl.from_arrow(pq.read_table(path, columns=["column_4"], pre_buffer=True))
            dist = col.select(L=pl.col("column_4").str.strip_chars().str.len_chars().fill_null(-1)).group_by("L").agg(pl.len()).sort("L")
            yield ShapeObservation(day, stream, {int(k): int(v) for k, v in dist.iter_rows()})


@dataclass(frozen=True)
class SymbolMismatch:
    day: Day
    only_in: dict[str, tuple[str, ...]]     # stream → 只出现在该流的标的


def symbol_mismatches(root: Path, days: Iterable[Day], streams: Iterable[Stream] = STREAMS) -> tuple[SymbolMismatch, ...]:
    """跨流标的集合差（停牌心跳、单边缺失都会在这里现形）；只读 _symbol 列。"""
    out = []
    for day in days:
        sets = {s: set(pc.unique(pq.read_table(root / parquet_relpath(s, day), columns=[SYMBOL_COL], pre_buffer=True)[SYMBOL_COL]).to_pylist())
                for s in streams}
        common = set.intersection(*sets.values())
        only = {s: tuple(sorted(v - common)) for s, v in sets.items() if v - common}
        if only:
            out.append(SymbolMismatch(day, only))
    return tuple(out)


@dataclass(frozen=True)
class ReadTiming:
    day: Day
    stream: Stream
    seconds: float
    rows: int
    bytes_read: int


def read_floor(store: RawStore, ledger: DefectLedger, days: Iterable[Day], streams: Iterable[Stream] = STREAMS) -> tuple[ReadTiming, ...]:
    """全字段读（含 dtype 还原）逐天逐流计时：任何提取的 IO + 还原下界。"""
    out = []
    for day in days:
        for stream in streams:
            names = tuple(f.name for f in FIELDS[stream] if f.name != "symbol")
            t0 = time.time()
            res = store.execute(plan(ReadRequest(stream, (day,), names), store.catalog(stream, (day,)), ledger))
            out.append(ReadTiming(day, stream, round(time.time() - t0, 2), res.stats.rows, res.stats.bytes_read))
    return tuple(out)


def preserve_days(root: Path) -> tuple[Day, ...]:
    """root 下三流都有文件的天（与 RawStore.days 同义，但不要求 manifest 目录）。"""
    sets = [{dt.datetime.strptime(p.stem.split("=")[1], "%Y%m%d").date() for p in (root / s).glob("date=*.parquet")} for s in STREAMS]
    return tuple(sorted(set.intersection(*sets)))
