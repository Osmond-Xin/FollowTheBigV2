"""原始层接口上的类型。接口 = 调用者必须知道的一切：不变量与错误模式写在各类型的 docstring 里。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ftbv2.core.raw.schema import Stream

if TYPE_CHECKING:
    import polars as pl

Day = dt.date
"""交易日。文件名 date=YYYYMMDD.parquet 由它派生；接口上只用 date，不用字符串。"""


@dataclass(frozen=True)
class Window:
    """自午夜起的毫秒半开区间 [start_ms, end_ms)。时间列在文件里是字符串、statistics 按字典序
    （rg0 的 min/max 是 ('100000040','95959440')，min > max），**时间窗永远不能下推**，只能扫描后过滤。"""

    start_ms: int
    end_ms: int


CONTINUOUS: tuple[Window, Window] = (
    Window((9 * 3600 + 30 * 60) * 1000, (11 * 3600 + 30 * 60) * 1000),
    Window(13 * 3600 * 1000, (14 * 3600 + 57 * 60) * 1000),
)
"""连续竞价 = 上午段 ∪ 下午段（剔除集合竞价与午休）。与 schema.AM_START_MS 等同源。"""


@dataclass(frozen=True)
class ReadRequest:
    """一次读取的全部意图。

    - days：要读的交易日，缺文件不是错误而是 Gap（见 ReadResult.gaps）；
    - symbols：None = 全部；给定时按 _symbol 下推裁剪 row group（文件已全局按 _symbol 排序且 statistics 完好）；
    - fields：语义字段名（schema.FIELDS）或 "raw:column_N"；顺序即输出列顺序；symbol 不必列出，总在首列；
    - windows：时间窗，None = 全天；多个窗取并集；需要 time 列参与过滤时计划会内部扩展投影，输出不含它。
    """

    stream: Stream
    days: tuple[Day, ...]
    fields: tuple[str, ...]
    symbols: frozenset[str] | None = None
    windows: tuple[Window, ...] | None = None


@dataclass(frozen=True)
class RowGroupMeta:
    index: int
    num_rows: int
    byte_size: int          # 压缩后字节，来自 parquet footer
    symbol_min: str
    symbol_max: str


@dataclass(frozen=True)
class FileMeta:
    path: Path
    stream: Stream
    day: Day
    num_rows: int
    columns: tuple[str, ...]            # 物理列名（含 _symbol）
    row_groups: tuple[RowGroupMeta, ...]


@dataclass(frozen=True)
class Catalog:
    """一批文件的 footer 元数据。由 RawStore.catalog() 读取（IO），此后 plan() 纯计算。
    缺文件的天不在 files 里，由 missing_days 记录——「查不到 = 没有」被禁止。"""

    stream: Stream
    files: tuple[FileMeta, ...]
    missing_days: tuple[Day, ...] = ()


@dataclass(frozen=True)
class FilePlan:
    path: Path
    day: Day
    row_groups: tuple[int, ...] | None   # None = 全部；否则只读这些（按 _symbol statistics 裁剪）
    columns: tuple[str, ...]             # 物理投影，可能比输出多（过滤或补丁所需），返回前裁掉


@dataclass(frozen=True)
class ScanPlan:
    """纯函数 plan() 的产出。CI 在不碰数据时断言它的性质：
    passes == 1 · pre_buffer is True · 给了 symbols 就有 row group 裁剪 · 列子集最小 · 时间窗只在 post_filters。"""

    request: ReadRequest
    files: tuple[FilePlan, ...]
    output_fields: tuple[str, ...]       # 输出列顺序：symbol 在首，其后按 request.fields
    post_filters: tuple[str, ...]        # 扫描后过滤的描述，取值 {"symbol_exact", "window"}
    patches: tuple[str, ...]             # 由缺陷账本触发的补丁代码（见 ledger.py），按天可能不同
    passes: int = 1
    pre_buffer: bool = True
    engine: str = "pyarrow"


class GapReason(Enum):
    DAY_MISSING = "day_missing"            # 该天没有这个 stream 的文件
    SYMBOL_ABSENT = "symbol_absent"        # 请求的标的当天在文件里不存在
    STREAM_PARTIAL = "stream_partial"      # 缺陷账本登记的救援日：该标的在别的 stream 有、在这个没有


@dataclass(frozen=True)
class Gap:
    """缺口是一等公民，必须带归因码；它是下游纯核入口的必需参数，接口里没有 ignore_missing。"""

    day: Day
    stream: Stream
    reason: GapReason
    symbol: str | None = None


@dataclass(frozen=True)
class ReadStats:
    """让「338× 字节削减」可观测：单标的一天只碰 680 个 row group 里的 2 个。"""

    row_groups_total: int
    row_groups_read: int
    bytes_total: int
    bytes_read: int
    rows: int


@dataclass(frozen=True)
class ReadResult:
    frame: pl.DataFrame     # 列 = plan.output_fields，dtype 按 schema.Kind，行序 = 文件序
    gaps: tuple[Gap, ...]
    stats: ReadStats


class Quality(Enum):
    """一天原始数据的可验证性分档（词汇表「原始层」条）。不得声称「逐行无损」。"""

    VERIFIED_BYTE_EXACT = "verified_byte_exact"   # 幸存 7z 覆盖的约 23%（2022 全年 + 202608）且已比对
    SELF_CONSISTENT = "self_consistent"           # 只验证了 preserve 层自洽
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class StreamReceipt:
    stream: Stream
    n_symbols: int
    n_rows_csv: int         # 独立计数：CSV 字节流里的换行数减表头，不是从 parquet 反推
    n_rows_parquet: int
    header: str             # CSV 首行原文（GBK 解码），让列语义从公理变成数据
    parquet_bytes: int
    sha256_csv: str         # 全部 CSV 按标的顺序拼接的 sha256


@dataclass(frozen=True)
class IngestReceipt:
    day: Day
    archive: Path
    streams: tuple[StreamReceipt, ...]      # 三个 stream 齐全才算完成
    dropped_by_prefix: dict[str, int] = field(default_factory=dict)   # 前缀 → 被宇宙筛选丢弃的标的数
