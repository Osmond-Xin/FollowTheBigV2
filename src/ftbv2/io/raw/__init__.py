"""原始层的 IO 层：摄取（7z → preserve）、存储访问（footer 目录、执行计划）、审计。逻辑尽量薄，主要职责是异常处理。
公开接口 = 本文件的 __all__（architecture.toml：io.raw）；跨模块只能从这里 import。"""

from ftbv2.io.raw.audit import (
    ReadTiming,
    ShapeObservation,
    StreamCompare,
    SymbolMismatch,
    compare_preserve,
    preserve_days,
    read_floor,
    scan_time_shapes,
    symbol_mismatches,
)
from ftbv2.io.raw.ingest import EMPTY_FILE_STREAMS, BatchOutcome, DayOutcome, canonical_frame, ingest, ingest_days
from ftbv2.io.raw.store import HANDLED_PATCHES, RawStore, meta_path

__all__ = [
    "EMPTY_FILE_STREAMS", "HANDLED_PATCHES", "BatchOutcome", "DayOutcome", "RawStore", "ReadTiming", "ShapeObservation",
    "StreamCompare", "SymbolMismatch", "canonical_frame", "compare_preserve", "ingest", "ingest_days", "meta_path",
    "preserve_days", "read_floor", "scan_time_shapes", "symbol_mismatches",
]
