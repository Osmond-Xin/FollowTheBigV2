"""原始层接口上的类型。接口 = 调用者必须知道的一切：不变量、校验与错误模式写在各类型的 docstring 与 __post_init__ 里。"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from ftbv2.core.raw.schema import Stream

if TYPE_CHECKING:                       # 纯核不 import pathlib：Path 只作类型出现（codex 实现时指出）
    from pathlib import Path

    import polars as pl

Day = dt.date
"""交易日。文件名 date=YYYYMMDD.parquet 由它派生；接口上只用 date，不用字符串。"""

MS_PER_DAY = 86_400_000
SYMBOL_RE = re.compile(r"^\d{6}\.(SZ|SH)$")


@dataclass(frozen=True)
class Window:
    """自午夜起的毫秒半开区间 [start_ms, end_ms)，0 ≤ start < end ≤ 86_400_000，否则 ValueError。
    时间列在文件里是字符串、statistics 按字典序（rg0 的 min/max 是 ('100000040','95959440')，min > max），
    **时间窗永远不能下推**，只能扫描后过滤。"""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if not (0 <= self.start_ms < self.end_ms <= MS_PER_DAY):
            raise ValueError(f"非法时间窗 [{self.start_ms}, {self.end_ms})")


CONTINUOUS_EXCL_AUCTIONS: tuple[Window, Window] = (
    Window((9 * 3600 + 30 * 60) * 1000, (11 * 3600 + 30 * 60) * 1000),
    Window(13 * 3600 * 1000, (14 * 3600 + 57 * 60) * 1000),
)
"""连续竞价两段 [09:30, 11:30) ∪ [13:00, 14:57)。**显式剔除**了 09:15–09:30 开盘集合竞价、11:30–13:00 午休、
14:57–15:00 收盘集合竞价。集合竞价里的撤单与虚假撮合本身是研究对象：要它们就传 windows=None 或自定义窗，
两种样本不同，预注册必须写明用的是哪个。与 schema.AM_START_MS 等同源。"""


@dataclass(frozen=True)
class ReadRequest:
    """一次读取的全部意图。构造时校验，非法即 ValueError：

    - days：非空；缺文件不是错误而是 Gap；输出按 days 给定的顺序拼接；
    - fields：非空，语义字段名（schema.FIELDS）；未登记名在 plan() 时 KeyError；顺序即输出列顺序；
      day 与 symbol 是保留列，总在最前，不必列出；
    - symbols：None = 全部；给定时须非空且每个匹配 ^\\d{6}\\.(SZ|SH)$；按 _symbol 下推裁剪 row group；
    - windows：None = 全天；给定时非空，多个窗取并集；过滤所需的时间列由计划内部扩展投影，输出不含它。
    """

    stream: Stream
    days: tuple[Day, ...]
    fields: tuple[str, ...]
    symbols: frozenset[str] | None = None
    windows: tuple[Window, ...] | None = None

    def __post_init__(self) -> None:
        if not self.days:
            raise ValueError("days 不能为空")
        if not self.fields:
            raise ValueError("fields 不能为空")
        if self.symbols is not None:
            if not self.symbols:
                raise ValueError("symbols 为空集：全部请用 None")
            bad = sorted(s for s in self.symbols if not SYMBOL_RE.match(s))
            if bad:
                raise ValueError(f"非法标的代码 {bad}")
        if self.windows is not None and not self.windows:
            raise ValueError("windows 为空元组：全天请用 None")


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
    """一个文件的扫描计划。row_groups 带完整元数据，execute 统计字节数时不必再解析 footer。"""

    path: Path
    day: Day
    columns: tuple[str, ...]                 # 物理投影，可能比输出多（过滤或补丁所需），返回前裁掉
    row_groups: tuple[RowGroupMeta, ...]     # 要读的 row group（未裁剪时 = 全部）
    pruned: bool                             # 是否按 _symbol statistics 裁剪过
    patches: tuple[str, ...]                 # 缺陷账本为**这一天这个 stream** 触发的补丁代码，按文件隔离
    total_row_groups: int
    total_bytes: int


@dataclass(frozen=True)
class ScanPlan:
    """纯函数 plan() 的产出。CI 在不碰数据时断言：每个文件只出现一次（单趟）· 给了 symbols 就裁剪 ·
    列子集最小 · 时间窗只在 post_filters · 补丁按文件隔离。执行器怎么读（pyarrow、pre_buffer）是 RawStore 的
    内部不变量，不是计划上的旋钮。"""

    request: ReadRequest
    files: tuple[FilePlan, ...]              # 顺序 = request.days 顺序（缺的天跳过）
    output_fields: tuple[str, ...]           # ("day", "symbol", *request.fields 去重)
    post_filters: tuple[str, ...]            # 取值 {"symbol_exact", "window"}
    ledger_sha256: str                       # 计划所依据的缺陷账本内容哈希：账本一变，同一份数据的结果可以不同，必须可归因


class GapReason(Enum):
    DAY_MISSING = "day_missing"            # 该天没有这个 stream 的文件
    SYMBOL_ABSENT = "symbol_absent"        # 请求的标的当天在这个 stream 的文件里不存在


@dataclass(frozen=True)
class Gap:
    """缺口是一等公民，必须带归因码；它是下游纯核入口的必需参数，接口里没有 ignore_missing。
    defects = 缺陷账本为该天该 stream 登记的缺陷码（如 rescue_partial）——只转述账本，不偷读别的 stream。"""

    day: Day
    stream: Stream
    reason: GapReason
    symbol: str | None = None
    defects: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadStats:
    """观测辅助，让「338× 字节削减」可见（单标的一天只碰 680 个 row group 里的 2 个）。
    数值来自 footer 元数据，不作为正确性判据。"""

    row_groups_total: int
    row_groups_read: int
    bytes_total: int
    bytes_read: int
    rows: int


@dataclass(frozen=True)
class ReadResult:
    frame: pl.DataFrame     # 列 = plan.output_fields；day 为 pl.Date，其余 dtype 按 schema.Kind；行序 = 文件序
    gaps: tuple[Gap, ...]
    stats: ReadStats


class Quality(Enum):
    """一天原始数据的可验证性分档（词汇表「原始层」条）。不得声称「逐行无损」。"""

    VERIFIED_BYTE_EXACT = "verified_byte_exact"   # 幸存 7z 覆盖的约 23%（2022 全年 + 202608）且已比对
    SELF_CONSISTENT = "self_consistent"           # 只验证了 preserve 层自洽
    UNVERIFIED = "unverified"                     # 没有 manifest 记录


@dataclass(frozen=True)
class StreamReceipt:
    stream: Stream
    n_symbols: int
    n_rows_csv: int         # 独立计数：CSV 里表头之后的非空行数，不是从 parquet 反推
    n_rows_parquet: int
    header: str             # CSV 首行原文（GBK 解码，去掉行尾换行），让列语义从公理变成数据
    parquet_bytes: int
    sha256_csv: str         # 规范帧的 sha256：按标的升序，每个标的贡献 symbol\\0header\\0body 三段（含长度前缀）


@dataclass(frozen=True)
class IngestReceipt:
    day: Day
    archive: Path
    archive_sha256: str                     # 幂等判据的一部分：同一天换一个归档或换前缀集必须失败，不能静默返回旧 receipt
    prefixes: tuple[str, ...]               # 摄取时的前缀筛选（Q15 显式例外）
    sevenzip_version: str
    streams: tuple[StreamReceipt, ...]      # 三个 stream 齐全才算完成
    dropped_by_prefix: dict[str, int] = field(default_factory=dict)   # 被筛掉的标的按前缀计数：丢弃是决策，不是静默
