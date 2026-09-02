"""摄取（架构图模块表）：一天的 7z → {root}/{stream}/date=YYYYMMDD.parquet + manifest。

只能承诺 preserve 层自洽，不得承诺「逐行无损」。但 V1 的验收缺陷这里全部修掉：
- 行数校验独立计数（CSV 字节流换行数减表头），与 parquet 行数不符 ⇒ RuntimeError，不写 manifest；
- 表头原文进 manifest（列语义从公理变成数据）；
- 原子写：先写 tmp 再 os.replace；幂等判据 = manifest 三 stream 齐全，不是「某个文件存在」；
- 输出布局与现有 preserve 逐位兼容：列名 column_1..N 全 large_string + _symbol 列（值 "002783.SZ"），
  行按 _symbol 升序、标的内保持 CSV 原序，row_group_size = schema.ROW_GROUP_ROWS，zstd；
- 7z 用 7zz 一趟流式解出（py7zr 会挂）；解到私有 mkdtemp 目录，用完即删；
- 归档条目校验：拒绝绝对路径、含 ".." 的路径、符号链接与硬链接、规范化后重复的路径；解出的每个文件 resolve 后必须在
  scratch 之下；违反即 RuntimeError 且 root 无任何改动；
- **保留全部标的，没有前缀筛选**：样本宇宙属于预注册，原始层删行是 F182 模式。receipt 按交易所计数；
- 幂等绑定来源：manifest 记录 archive_sha256 与 7zz 版本；同一天再次摄取时归档哈希不同 ⇒ RuntimeError，不静默返回旧 receipt；
- 行数独立计数 = 表头之后的非空行数（末尾多余换行不算行）；
- sha256_csv 的输入是规范帧：按标的升序，每个标的贡献 len(symbol)\0symbol\0len(header)\0header\0len(body)\0body。
"""

from __future__ import annotations

from pathlib import Path

from ftbv2.core.raw.types import Day, IngestReceipt


def ingest(day: Day, archive: Path, root: Path, *, scratch_parent: Path | None = None) -> IngestReceipt:
    """archive 内布局 {YYYYMMDD}/{symbol}/{行情,逐笔委托,逐笔成交}.csv（也接受无日期前缀的扁平布局）。
    CSV：GBK 表头一行 + 纯 ASCII 数据行，所有字段按字符串原样保留（含 '\\x00'）。
    已完成（manifest 三 stream 齐全且 archive_sha256 相同）的天直接返回既有 receipt，不重做。
    scratch_parent：临时解包目录的父目录（默认系统临时目录）；实际解包目录用 mkdtemp 私有创建。"""
    raise NotImplementedError
