"""原始层存储与访问（架构图模块表）。接口：catalog · execute · days · quality。plan() 是纯函数，在 core.raw.plan。

不变量：
- root 未挂载 / 缺 stream 目录 ⇒ 构造时即 RuntimeError（fail-loud，F19）；绝不返回空结果假装没数据；
- 返回行序 = 文件序；
- 只走 pyarrow `read_table(..., pre_buffer=True)`（单列全文件扫描 231.9 s → 5.3 s），再 `pl.from_arrow`；
- 未登记形状硬失败：账本未登记 time_6digit 的天出现六位时间 ⇒ RuntimeError 并指出天与 stream。
  只对本次投影实际读到的时间列检查（列裁剪不为此破例）；全字段的形状扫描是摄取与离线校验工具的职责；
- 输出多日时以 day 列区分（time_ms 每天从午夜归零）；
- root 以 resolve(strict=True) 解析；manifest 存在但损坏 ⇒ RuntimeError，不存在 ⇒ UNVERIFIED；
- 接口上没有 force / ignore_missing / relax。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ftbv2.core.raw.ledger import DefectLedger
from ftbv2.core.raw.schema import Stream
from ftbv2.core.raw.types import Catalog, Day, Quality, ReadResult, ScanPlan

if TYPE_CHECKING:
    import polars as pl


class RawStore:
    """root 下的布局：{root}/{stream}/date=YYYYMMDD.parquet，{root}/manifest/YYYYMMDD.json（V2 摄取写）。"""

    def __init__(self, root: Path, ledger: DefectLedger) -> None:
        from ftbv2.io.raw._store_impl import init

        init(self, root, ledger)

    def catalog(self, stream: Stream, days: tuple[Day, ...]) -> Catalog:
        """只读 footer（每文件一次），不读数据。缺文件的天进 missing_days。"""
        from ftbv2.io.raw._store_impl import catalog

        return catalog(self, stream, days)

    def execute(self, plan: ScanPlan) -> ReadResult:
        """按计划读取：只读 plan.files 指定的 row group 与列；扫描后过滤；按 FilePlan.patches 逐文件解码；裁到 output_fields。
        缺口归因只用本 stream 的事实与账本：天缺文件 → DAY_MISSING；请求的标的不在文件里 → SYMBOL_ABSENT，
        并把账本为该天该 stream 登记的缺陷码（如 rescue_partial）放进 Gap.defects——不偷读别的 stream。
        stats 来自 FilePlan 里的 footer 元数据。"""
        from ftbv2.io.raw._store_impl import execute

        return execute(self, plan)

    def inspect_raw(self, stream: Stream, day: Day, columns: tuple[str, ...],
                    symbols: frozenset[str] | None = None) -> pl.DataFrame:
        """给人看的旁路：按物理列名（column_N / _symbol）原样返回字符串，不经 schema、不经账本、不经计划。
        用于登记新列前的探查。因子与事件提取不得调用（import-linter 契约待加）。"""
        from ftbv2.io.raw._store_impl import inspect_raw

        return inspect_raw(self, stream, day, columns, symbols)

    def days(self, quality: Quality | None = None) -> tuple[Day, ...]:
        """三个 stream 都有文件的天，升序。quality 给定时过滤；无 manifest 的天算 UNVERIFIED，
        所以 day ∈ days(quality(day)) 对任何一天都成立。"""
        from ftbv2.io.raw._store_impl import days

        return days(self, quality)

    def quality(self, day: Day) -> Quality:
        """manifest 里没有记录 ⇒ UNVERIFIED，不是异常。"""
        from ftbv2.io.raw._store_impl import quality

        return quality(self, day)
