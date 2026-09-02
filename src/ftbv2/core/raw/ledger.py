"""缺陷账本：按天变化的数据缺陷是**登记数据**，不是散在代码里的 if。未登记的形状一律硬失败。

账本文件 ledger/defects.toml（纯数据，git 跟踪）。本模块只解析文本（纯），读文件是 IO 层的事。
数据表第四节是账本内容的来源；红队修正：IO 阶段只能基于账本与元数据分支，不能基于研究统计结果分支。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import tomllib
from dataclasses import dataclass
from enum import Enum

from ftbv2.core.raw.schema import STREAMS, Stream


class DefectCode(Enum):
    TIME_6DIGIT = "time_6digit"            # column_4 混有 HHMMSS 六位；补丁：按行长度归一化 ×1000
    SEQ_EMPTY = "seq_empty"                # trades column_5 整列空（厂商侧）
    SEQ_SPARSE_DUP = "seq_sparse_dup"      # trades column_5 稀疏重复键
    RESCUE_PARTIAL = "rescue_partial"      # 7z Data Error 后救援入库：orders / trades 标的集合不一致
    ENUM_DRIFT = "enum_drift"              # 2025 起枚举新值（' ' S / C I J O）——只登记事实，读取不做映射
    INT32_OVERFLOW = "int32_overflow"      # xinqing column_13 超 int32，必须 int64
    NUL_SENTINEL_SH = "nul_sentinel_sh"    # SH trades column_6/7 全为 '\x00'


@dataclass(frozen=True)
class Defect:
    id: str                        # 稳定标识；账本 append-only：改一条 = 新增一条并 supersedes 旧 id（CI 门禁待建，纪律先立）
    code: DefectCode
    stream: Stream | None          # None = 三个 stream 都受影响
    days: tuple[dt.date, ...]      # 空元组 = 结构性、对所有天成立（如 NUL 哨兵、int32 溢出）
    note: str = ""


@dataclass(frozen=True)
class DefectLedger:
    entries: tuple[Defect, ...]
    sha256: str                    # 账本文本的内容哈希，进 ScanPlan / 证据指纹

    def for_day(self, day: dt.date, stream: Stream) -> tuple[Defect, ...]:
        """该天该 stream 登记在案的缺陷（含全天性条目），顺序 = 账本顺序。"""
        return tuple(
            d for d in self.entries
            if (d.stream is None or d.stream == stream) and (not d.days or day in d.days)
        )

    def has(self, day: dt.date, stream: Stream, code: DefectCode) -> bool:
        return any(d.code == code for d in self.for_day(day, stream))


def parse_ledger(text: str) -> DefectLedger:
    """解析 defects.toml 文本。格式：

    [[defect]]
    id = "D001"              # 必填、唯一
    code = "time_6digit"
    stream = "orders"        # 可省略 = 全部 stream
    days = [2024-02-06]      # 可省略 = 结构性
    note = "…"
    """
    data = tomllib.loads(text)
    entries = []
    seen: set[str] = set()
    for row in data.get("defect", []):
        ident = str(row["id"])
        if ident in seen:
            raise ValueError(f"账本 id 重复：{ident}")
        seen.add(ident)
        days = tuple(dt.date.fromisoformat(str(d)) if not isinstance(d, dt.date) else d for d in row.get("days", []))
        stream = row.get("stream")
        if stream is not None and stream not in STREAMS:
            raise ValueError(f"账本里未知的 stream {stream!r}（合法值 {STREAMS}）")
        entries.append(Defect(ident, DefectCode(row["code"]), stream, days, row.get("note", "")))
    return DefectLedger(tuple(entries), hashlib.sha256(text.encode("utf-8")).hexdigest())
