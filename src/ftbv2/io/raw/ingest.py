"""摄取（架构图模块表）：一天的 7z → {root}/{stream}/date=YYYYMMDD.parquet + manifest。

只能承诺 preserve 层自洽，不得承诺「逐行无损」。但 V1 的验收缺陷这里全部修掉：
- 行数校验独立计数（CSV 字节流换行数减表头），与 parquet 行数不符 ⇒ RuntimeError，不写 manifest；
- 表头原文进 manifest（列语义从公理变成数据）；
- 原子写：先写 tmp 再 os.replace；幂等判据 = manifest 三 stream 齐全，不是「某个文件存在」；
- 输出布局与现有 preserve 逐位兼容：列名 column_1..N 全 large_string + _symbol 列（值 "002783.SZ"），
  行按 _symbol 升序、标的内保持 CSV 原序，row_group_size = schema.ROW_GROUP_ROWS，zstd；
- 7z 用 7zz 一趟流式解出（py7zr 会挂）；解包目录用完即删；
- 宇宙筛选按前缀（默认 MAIN_PREFIXES），被丢弃的按前缀计数进 receipt——丢弃是决策（Q15），不是静默。
"""

from __future__ import annotations

from pathlib import Path

from ftbv2.core.raw.schema import MAIN_PREFIXES
from ftbv2.core.raw.types import Day, IngestReceipt


def ingest(
    day: Day,
    archive: Path,
    root: Path,
    *,
    prefixes: tuple[str, ...] = MAIN_PREFIXES,
    scratch: Path | None = None,
) -> IngestReceipt:
    """archive 内布局 {YYYYMMDD}/{symbol}/{行情,逐笔委托,逐笔成交}.csv（也接受无日期前缀的扁平布局）。
    CSV：GBK 表头一行 + 纯 ASCII 数据行，所有字段按字符串原样保留（含 '\\x00'）。
    已完成（manifest 三 stream 齐全）的天直接返回既有 receipt，不重做。"""
    raise NotImplementedError
