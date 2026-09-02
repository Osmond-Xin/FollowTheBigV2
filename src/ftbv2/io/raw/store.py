"""原始层存储与访问（架构图模块表）。接口：catalog · execute · days · quality · inspect_raw。plan() 是纯函数，在 core.raw.plan。

不变量：
- root 以 resolve(strict=True) 解析；未挂载 / 缺 stream 目录 ⇒ 构造时即 RuntimeError（fail-loud，F19），消息指出路径与 stream；
  绝不返回空结果假装没数据；
- 返回行序 = 文件序；
- 只走 pre_buffer=True 的 pyarrow 读（ParquetFile.read_row_groups / read_table），再 `pl.from_arrow`；
  禁止 pl.read_parquet(use_pyarrow=True)（实测慢 13 倍）；
- 全天扫描是 CPU 绑定的 dtype 还原，不是 IO（2026-09-02 真实文件复测）：要还原的列先 strip 物化一次再交给 decode
  （pre_stripped=True），present 集合先 unique 再落 Python——不做 Python 对象往返；
- 未登记形状硬失败：账本未登记 time_6digit 的天出现六位时间 ⇒ RuntimeError 并指出天与 stream；
  非空 row group 缺 _symbol statistics 的文件不是登记的形状 ⇒ catalog 时 RuntimeError（绝不静默裁掉真实数据）；
  只对本次投影实际读到的时间列检查（列裁剪不为此破例）；全字段的形状扫描是摄取与离线校验工具的职责；
  stream 目录里不符合 date=YYYYMMDD.parquet 的文件同样硬失败；
- 输出多日时以 day 列区分（time_ms 每天从午夜归零）；
- manifest 不存在 ⇒ UNVERIFIED；存在但损坏（非 JSON、非对象、缺 quality、值未登记）⇒ RuntimeError；
- 接口上没有 force / ignore_missing / relax。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

from ftbv2.core.raw.decode import decode_field, in_windows, output_dtype, short_time_present, strip_columns
from ftbv2.core.raw.ledger import DefectLedger
from ftbv2.core.raw.plan import attribute_gaps
from ftbv2.core.raw.schema import FIELDS, STREAMS, SYMBOL_COL, Stream, field, manifest_relpath, parquet_relpath
from ftbv2.core.raw.types import (
    Catalog,
    Day,
    FileMeta,
    FilePlan,
    Quality,
    ReadResult,
    ReadStats,
    RowGroupMeta,
    ScanPlan,
)


HANDLED_PATCHES = frozenset({"time_6digit"})   # 读取层真有处理器的补丁码；账本门禁的 KNOWN_PATCH_CODES 与之相等（测试断言）


class RawStore:
    """root 下的布局：{root}/{stream}/date=YYYYMMDD.parquet，{root}/manifest/YYYYMMDD.json（V2 摄取写）。"""

    def __init__(self, root: Path, ledger: DefectLedger) -> None:
        try:
            self._root = root.resolve(strict=True)
        except FileNotFoundError as exc:
            raise RuntimeError(f"原始层未挂载：{root}") from exc
        for stream in STREAMS:
            if not (self._root / stream).is_dir():
                raise RuntimeError(f"原始层缺 stream 目录：{stream}（{self._root}）")
        self._ledger = ledger

    def catalog(self, stream: Stream, days: tuple[Day, ...]) -> Catalog:
        """只读 footer（每文件一次），不读数据。缺文件的天进 missing_days。空 row group 记为无区间（不会被选中）。"""
        files, missing = [], []
        for day in days:
            path = self._root / parquet_relpath(stream, day)
            if not path.exists():
                missing.append(day)
                continue
            meta = pq.read_metadata(path)
            columns = tuple(meta.schema.names)
            symbol_index = columns.index(SYMBOL_COL)
            try:
                row_groups = tuple(_row_group_meta(meta, i, symbol_index) for i in range(meta.num_row_groups))
            except RuntimeError as exc:
                raise RuntimeError(f"{path}：{exc}") from exc
            files.append(FileMeta(path, stream, day, meta.num_rows, columns, row_groups))
        return Catalog(stream, tuple(files), tuple(missing))

    def execute(self, plan: ScanPlan) -> ReadResult:
        """按计划读取：只读 plan.files 指定的 row group 与列；扫描后过滤；按 FilePlan.patches 逐文件解码；裁到 output_fields。
        缺口归因见 core.raw.plan.attribute_gaps。stats 来自 FilePlan 里的 footer 元数据。"""
        request = plan.request
        time_column = field(request.stream, "time_ms").column
        frames: list[pl.DataFrame] = []
        present: dict[Day, frozenset[str]] = {}
        to_strip = [f.column for f in FIELDS[request.stream] if strip_columns(f)]
        for fp in plan.files:
            unknown = sorted(set(fp.patches) - HANDLED_PATCHES)
            if unknown:
                raise RuntimeError(f"账本给 {fp.day:%Y-%m-%d} {request.stream} 的补丁码没有处理器：{unknown}（计划显示已触发，执行层绝不静默忽略）")
            raw = _read_row_groups(fp)
            raw = raw.with_columns([pl.col(c).str.strip_chars() for c in to_strip if c in raw.columns])   # 物化一次，见模块 docstring
            if time_column in raw.columns and "time_6digit" not in fp.patches and raw.select(short_time_present(time_column, pre_stripped=True)).item():
                raise RuntimeError(f"time_6digit 未登记：{fp.day:%Y-%m-%d} {request.stream} 出现六位时间")
            present[fp.day] = frozenset(raw[SYMBOL_COL].drop_nulls().unique().to_list())
            frames.append(_decode(raw, fp, plan))
        frame = pl.concat(frames, how="vertical") if frames else _empty_frame(plan)
        gaps = attribute_gaps(request, frozenset(fp.day for fp in plan.files), present, self._ledger)
        stats = ReadStats(
            sum(fp.total_row_groups for fp in plan.files),
            sum(len(fp.row_groups) for fp in plan.files),
            sum(fp.total_bytes for fp in plan.files),
            sum(rg.byte_size for fp in plan.files for rg in fp.row_groups),
            frame.height,
        )
        return ReadResult(frame, gaps, stats)

    def inspect_raw(self, stream: Stream, day: Day, columns: tuple[str, ...],
                    symbols: frozenset[str] | None = None) -> pl.DataFrame:
        """给人看的旁路：按物理列名（column_N / _symbol）原样返回字符串，不经 schema、不经账本、不经计划。
        用于登记新列前的探查。因子与事件提取不得调用（import-linter 契约待加）。"""
        projection = list(columns)
        if symbols is not None and SYMBOL_COL not in projection:
            projection.append(SYMBOL_COL)
        table = pq.read_table(self._root / parquet_relpath(stream, day), columns=projection, pre_buffer=True)
        frame = pl.from_arrow(table)
        if symbols is not None:
            frame = frame.filter(pl.col(SYMBOL_COL).is_in(sorted(symbols)))
        return frame.select(list(columns))

    def days(self, quality: Quality | None = None) -> tuple[Day, ...]:
        """三个 stream 都有文件的天，升序。quality 给定时过滤；无 manifest 的天算 UNVERIFIED，
        所以 day ∈ days(quality(day)) 对任何一天都成立。"""
        common = set.intersection(*(_stream_days(self._root / s) for s in STREAMS))
        ordered = tuple(sorted(common))
        if quality is None:
            return ordered
        return tuple(d for d in ordered if self.quality(d) is quality)

    def quality(self, day: Day) -> Quality:
        """manifest 里没有记录 ⇒ UNVERIFIED，不是异常；存在但不可信 ⇒ RuntimeError。"""
        path = self._root / manifest_relpath(day)
        if not path.exists():
            return Quality.UNVERIFIED
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Quality(data["quality"])
        except (json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
            raise RuntimeError(f"manifest 损坏：{path}（{exc}）") from exc


def _row_group_meta(meta: pq.FileMetaData, index: int, symbol_index: int) -> RowGroupMeta:
    group = meta.row_group(index)
    stats = group.column(symbol_index).statistics
    byte_size = sum(group.column(i).total_compressed_size for i in range(group.num_columns))
    if group.num_rows == 0:
        return RowGroupMeta(index, 0, byte_size, "", "")
    if stats is None or not stats.has_min_max or stats.min is None or stats.max is None:
        raise RuntimeError(f"{meta_path(meta)} row group {index} 非空却没有 _symbol statistics，不是登记的形状")
    return RowGroupMeta(index, group.num_rows, byte_size, stats.min, stats.max)


def meta_path(meta: pq.FileMetaData) -> str:
    return getattr(meta, "_path", "parquet")


def _read_row_groups(fp: FilePlan) -> pl.DataFrame:
    if not fp.row_groups:
        return pl.DataFrame({c: pl.Series(c, [], dtype=pl.String) for c in fp.columns})
    table = pq.ParquetFile(fp.path, pre_buffer=True).read_row_groups(
        [rg.index for rg in fp.row_groups], columns=list(fp.columns)
    )
    return pl.from_arrow(table)


def _decode(raw: pl.DataFrame, fp: FilePlan, plan: ScanPlan) -> pl.DataFrame:
    """物理列 → 语义列，扫描后过滤，裁到输出列。过滤所需的 time_ms 若不在输出里，最后被裁掉。"""
    request = plan.request
    allow_6digit = "time_6digit" in fp.patches
    needed = list(plan.output_fields)
    if request.windows is not None and "time_ms" not in needed:
        needed.append("time_ms")
    exprs = [pl.lit(fp.day).cast(pl.Date).alias("day"), pl.col(SYMBOL_COL).alias("symbol")]
    exprs += [
        decode_field(field(request.stream, name), allow_6digit=allow_6digit, pre_stripped=True).alias(name)
        for name in needed if name not in ("day", "symbol")
    ]
    frame = raw.with_columns(exprs)
    if request.symbols is not None:
        frame = frame.filter(pl.col("symbol").is_in(sorted(request.symbols)))
    if request.windows is not None:
        frame = frame.filter(in_windows("time_ms", request.windows))
    return frame.select(list(plan.output_fields))


def _empty_frame(plan: ScanPlan) -> pl.DataFrame:
    stream = plan.request.stream
    dtypes = {
        name: pl.Date() if name == "day" else pl.String() if name == "symbol" else output_dtype(field(stream, name).kind)
        for name in plan.output_fields
    }
    return pl.DataFrame({name: pl.Series(name, [], dtype=dtype) for name, dtype in dtypes.items()})


def _stream_days(stream_dir: Path) -> set[dt.date]:
    out: set[dt.date] = set()
    for entry in stream_dir.iterdir():
        if entry.name.startswith(".") or entry.name.endswith(".tmp"):
            continue
        try:
            out.add(dt.datetime.strptime(entry.name, "date=%Y%m%d.parquet").date())
        except ValueError as exc:
            raise RuntimeError(f"原始层目录里有未登记形状的文件：{entry}") from exc
    return out
