"""原始层存储与访问（架构图模块表）。接口：catalog · execute · days · quality。plan() 是纯函数，在 core.raw.plan。

不变量：
- root 未挂载 / 缺 stream 目录 ⇒ 构造时即 RuntimeError（fail-loud，F19）；绝不返回空结果假装没数据；
- 返回行序 = 文件序；
- 只走 pyarrow `read_table(..., pre_buffer=True)`（单列全文件扫描 231.9 s → 5.3 s），再 `pl.from_arrow`；
- 未登记形状硬失败：账本未登记 time_6digit 的天出现六位时间 ⇒ RuntimeError 并指出天与 stream；
- 接口上没有 force / ignore_missing / relax。
"""

from __future__ import annotations

from pathlib import Path

from ftbv2.core.raw.ledger import DefectLedger
from ftbv2.core.raw.schema import Stream
from ftbv2.core.raw.types import Catalog, Day, Quality, ReadResult, ScanPlan


class RawStore:
    """root 下的布局：{root}/{stream}/date=YYYYMMDD.parquet，{root}/manifest/YYYYMMDD.json（V2 摄取写）。"""

    def __init__(self, root: Path, ledger: DefectLedger) -> None:
        raise NotImplementedError

    def catalog(self, stream: Stream, days: tuple[Day, ...]) -> Catalog:
        """只读 footer（每文件一次），不读数据。缺文件的天进 missing_days。"""
        raise NotImplementedError

    def execute(self, plan: ScanPlan) -> ReadResult:
        """按计划读取：只读 plan.files 指定的 row group 与列；扫描后过滤；dtype 还原；裁到 output_fields。
        stats.row_groups_read / bytes_read 来自 footer 元数据，不是估算。"""
        raise NotImplementedError

    def days(self, quality: Quality | None = None) -> tuple[Day, ...]:
        """三个 stream 都有文件的天，升序；quality 给定时按 manifest 过滤。"""
        raise NotImplementedError

    def quality(self, day: Day) -> Quality:
        """manifest 里没有记录 ⇒ UNVERIFIED，不是异常。"""
        raise NotImplementedError
