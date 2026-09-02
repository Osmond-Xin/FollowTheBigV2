"""原始层的纯逻辑核：schema 公理、类型、缺陷账本、dtype 还原、扫描计划。不触碰 IO。
公开接口 = 本文件的 __all__（architecture.toml：core.raw）；跨模块只能从这里 import。"""

from ftbv2.core.raw.decode import decode_field, in_windows, output_dtype, short_time_present, strip_columns, to_int64, to_time_ms
from ftbv2.core.raw.ledger import ACTIONS, KINDS, LIVE, STATUSES, Defect, DefectCode, DefectLedger, parse_ledger
from ftbv2.core.raw.plan import attribute_gaps, plan
from ftbv2.core.raw.schema import (
    AM_END_MS,
    AM_START_MS,
    CSV_NAME,
    FIELDS,
    MAIN_PREFIXES,
    PM_END_MS,
    PM_START_MS,
    PRICE_SCALE,
    ROW_GROUP_ROWS,
    STREAMS,
    SYMBOL_COL,
    Field,
    Kind,
    Stream,
    archive_day,
    field,
    manifest_relpath,
    parquet_relpath,
)
from ftbv2.core.raw.types import (
    CONTINUOUS_EXCL_AUCTIONS,
    Catalog,
    Day,
    FileMeta,
    FilePlan,
    Gap,
    GapReason,
    IngestReceipt,
    Quality,
    ReadRequest,
    ReadResult,
    ReadStats,
    RowGroupMeta,
    ScanPlan,
    StreamReceipt,
    Window,
)

__all__ = [
    "ACTIONS", "AM_END_MS", "AM_START_MS", "CONTINUOUS_EXCL_AUCTIONS", "CSV_NAME", "Catalog", "Day", "Defect", "DefectCode",
    "DefectLedger", "FIELDS", "Field", "FileMeta", "FilePlan", "Gap", "GapReason", "IngestReceipt", "KINDS", "Kind", "LIVE",
    "MAIN_PREFIXES", "PM_END_MS", "PM_START_MS", "PRICE_SCALE", "Quality", "ROW_GROUP_ROWS", "ReadRequest", "ReadResult",
    "ReadStats", "RowGroupMeta", "STATUSES", "STREAMS", "SYMBOL_COL", "ScanPlan", "Stream", "StreamReceipt", "Window",
    "archive_day", "attribute_gaps", "decode_field", "field", "in_windows", "manifest_relpath", "output_dtype", "parquet_relpath",
    "parse_ledger", "plan", "short_time_present", "strip_columns", "to_int64", "to_time_ms",
]
