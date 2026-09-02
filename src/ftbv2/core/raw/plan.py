"""扫描计划与缺口归因：纯函数。CI 在不碰任何数据时断言计划的性质，这是「CI 拿不到 TB 数据」死结的一半解法（收敛点 01）。"""

from __future__ import annotations

from ftbv2.core.raw.ledger import DefectLedger
from ftbv2.core.raw.schema import SYMBOL_COL, field
from ftbv2.core.raw.types import (
    Catalog,
    Day,
    FileMeta,
    FilePlan,
    Gap,
    GapReason,
    ReadRequest,
    RowGroupMeta,
    ScanPlan,
)

RESERVED = ("day", "symbol")


def plan(request: ReadRequest, catalog: Catalog, ledger: DefectLedger) -> ScanPlan:
    """由请求 + 目录元数据 + 缺陷账本算出扫描计划。

    必须满足（契约测试逐条断言）：
    - 每个文件只出现一次（单趟）；files 顺序 = request.days 顺序，catalog.missing_days 里的天跳过（缺口由 execute 归因）；
    - request.symbols 给定时，FilePlan.row_groups 只含 [symbol_min, symbol_max] 与 symbols 相交的 row group，pruned=True；
      symbols 为 None 时 row_groups = 全部，pruned=False；空 row group（无 statistics）永不入选；
    - 时间窗永远不下推：windows 给定 ⇒ "window" ∈ post_filters，且 row_groups 不因 windows 变化；
      symbols 给定 ⇒ "symbol_exact" ∈ post_filters（statistics 裁剪是区间，精确匹配在扫描后）；
    - 物理投影 = 输出字段的列 ∪ 过滤所需列（symbols ⇒ _symbol；windows ⇒ 时间列），顺序稳定；
      output_fields = ("day", "symbol", *request.fields 去重且剔除这两个保留名)；
    - 补丁按文件隔离：FilePlan.patches = ledger.patches(那一天, 那个 stream)；
      未登记而数据里出现六位时间是 execute 的硬失败，不在这里；
    - ScanPlan.ledger_sha256 = ledger.sha256；
    - 未登记字段名 ⇒ KeyError（来自 schema.field），不静默；
    - catalog.stream 必须等于 request.stream，且 catalog 必须覆盖 request.days 的每一天（在 files 或 missing_days 里），否则 ValueError。
    """
    if catalog.stream != request.stream:
        raise ValueError(f"stream 不一致：request={request.stream} catalog={catalog.stream}")
    covered = {m.day for m in catalog.files} | set(catalog.missing_days)
    uncovered = sorted(set(request.days) - covered)
    if uncovered:
        raise ValueError(f"catalog 未覆盖请求的天：{uncovered}")
    output_tail, projected = _output_and_projection(request)
    by_day = {meta.day: meta for meta in catalog.files}
    missing = set(catalog.missing_days)
    files = tuple(
        _file_plan(request, by_day[day], ledger, projected)
        for day in request.days
        if day not in missing and day in by_day
    )
    return ScanPlan(request, files, (*RESERVED, *output_tail), _post_filters(request), ledger.sha256)


def attribute_gaps(
    request: ReadRequest,
    file_days: frozenset[Day],
    present_by_day: dict[Day, frozenset[str]],
    ledger: DefectLedger,
) -> tuple[Gap, ...]:
    """缺口归因，只用本 stream 的事实：缺文件的天 → 一条 DAY_MISSING（不为标的展开）；
    文件存在而请求的标的不在其中 → SYMBOL_ABSENT。defects 只转述账本按天登记的条目。"""
    gaps: list[Gap] = []
    for day in request.days:
        defects = ledger.day_scoped_codes(day, request.stream)
        if day not in file_days:
            gaps.append(Gap(day, request.stream, GapReason.DAY_MISSING, None, defects))
            continue
        if request.symbols is None:
            continue
        present = present_by_day.get(day, frozenset())
        gaps.extend(
            Gap(day, request.stream, GapReason.SYMBOL_ABSENT, symbol, defects)
            for symbol in sorted(request.symbols)
            if symbol not in present
        )
    return tuple(gaps)


def _output_and_projection(request: ReadRequest) -> tuple[tuple[str, ...], tuple[str, ...]]:
    tail: list[str] = []
    projected = [SYMBOL_COL]
    for name in request.fields:
        if name in RESERVED or name in tail:
            continue
        column = field(request.stream, name).column
        tail.append(name)
        if column not in projected:
            projected.append(column)
    if request.windows is not None:
        time_column = field(request.stream, "time_ms").column
        if time_column not in projected:
            projected.append(time_column)
    return tuple(tail), tuple(projected)


def _post_filters(request: ReadRequest) -> tuple[str, ...]:
    filters = []
    if request.symbols is not None:
        filters.append("symbol_exact")
    if request.windows is not None:
        filters.append("window")
    return tuple(filters)


def _file_plan(request: ReadRequest, meta: FileMeta, ledger: DefectLedger, projected: tuple[str, ...]) -> FilePlan:
    row_groups, pruned = _select_row_groups(meta.row_groups, request.symbols)
    return FilePlan(
        meta.path, meta.day, projected, row_groups, pruned,
        ledger.patches(meta.day, request.stream),
        len(meta.row_groups), sum(rg.byte_size for rg in meta.row_groups),
    )


def _select_row_groups(
    row_groups: tuple[RowGroupMeta, ...], symbols: frozenset[str] | None
) -> tuple[tuple[RowGroupMeta, ...], bool]:
    if symbols is None:
        return row_groups, False
    chosen = tuple(
        rg for rg in row_groups
        if rg.num_rows > 0 and any(rg.symbol_min <= s <= rg.symbol_max for s in symbols)
    )
    return chosen, True
