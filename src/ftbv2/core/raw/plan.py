"""扫描计划：纯函数。CI 在不碰任何数据时断言计划的性质，这是「CI 拿不到 TB 数据」死结的一半解法（收敛点 01）。"""

from __future__ import annotations

from ftbv2.core.raw.ledger import DefectLedger
from ftbv2.core.raw.types import Catalog, ReadRequest, ScanPlan


def plan(request: ReadRequest, catalog: Catalog, ledger: DefectLedger) -> ScanPlan:
    """由请求 + 目录元数据 + 缺陷账本算出扫描计划。

    必须满足（契约测试逐条断言）：
    - 每个文件只出现一次（单趟）；files 顺序 = request.days 顺序，catalog.missing_days 里的天跳过（缺口由 execute 归因）；
    - request.symbols 给定时，FilePlan.row_groups 只含 [symbol_min, symbol_max] 与 symbols 相交的 row group，pruned=True；
      symbols 为 None 时 row_groups = 全部，pruned=False；
    - 时间窗永远不下推：windows 给定 ⇒ "window" ∈ post_filters，且 row_groups 不因 windows 变化；
      symbols 给定 ⇒ "symbol_exact" ∈ post_filters（statistics 裁剪是区间，精确匹配在扫描后）；
    - 物理投影 = 输出字段的列 ∪ 过滤所需列（symbols ⇒ _symbol；windows ⇒ column_4）∪ 补丁所需列，顺序稳定；
      output_fields = ("day", "symbol", *request.fields 去重且剔除这两个保留名)；
    - 补丁按文件隔离：只有账本为**那一天那个 stream** 登记 time_6digit 的 FilePlan.patches 含 "time_6digit"；
      未登记而数据里出现六位时间是 execute 的硬失败，不在这里；
    - ScanPlan.ledger_sha256 = ledger.sha256；
    - 未登记字段名 ⇒ KeyError（来自 schema.field），不静默。
    """
    from ftbv2.core.raw._plan_impl import make_plan

    return make_plan(request, catalog, ledger)
