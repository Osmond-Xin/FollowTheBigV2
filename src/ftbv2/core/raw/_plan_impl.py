from __future__ import annotations

from ftbv2.core.raw.ledger import DefectLedger
from ftbv2.core.raw.schema import SYMBOL_COL, field
from ftbv2.core.raw.types import Catalog, FileMeta, FilePlan, ReadRequest, RowGroupMeta, ScanPlan


def make_plan(request: ReadRequest, catalog: Catalog, ledger: DefectLedger) -> ScanPlan:
    output_tail, projected = _output_and_projection(request)
    files = _file_plans(request, catalog, ledger, tuple(projected))
    return ScanPlan(
        request,
        files,
        ("day", "symbol", *output_tail),
        _post_filters(request),
        ledger.sha256,
    )


def _output_and_projection(request: ReadRequest) -> tuple[tuple[str, ...], list[str]]:
    output_tail = []
    seen = {"day", "symbol"}
    projected = [SYMBOL_COL]
    for name in request.fields:
        if name in seen:
            continue
        schema_field = field(request.stream, name)
        output_tail.append(name)
        seen.add(name)
        if schema_field.column not in projected:
            projected.append(schema_field.column)
    if request.windows is not None:
        _append_once(projected, field(request.stream, "time_ms").column)
    return tuple(output_tail), projected


def _append_once(columns: list[str], column: str) -> None:
    if column not in columns:
        columns.append(column)


def _post_filters(request: ReadRequest) -> tuple[str, ...]:
    filters = []
    if request.symbols is not None:
        filters.append("symbol_exact")
    if request.windows is not None:
        filters.append("window")
    return tuple(filters)


def _file_plans(
    request: ReadRequest,
    catalog: Catalog,
    ledger: DefectLedger,
    projected: tuple[str, ...],
) -> tuple[FilePlan, ...]:
    files_by_day = {meta.day: meta for meta in catalog.files}
    missing_days = set(catalog.missing_days)
    return tuple(
        _file_plan(request, files_by_day[day], ledger, projected)
        for day in request.days
        if day not in missing_days and day in files_by_day
    )


def _file_plan(
    request: ReadRequest,
    meta: FileMeta,
    ledger: DefectLedger,
    projected: tuple[str, ...],
) -> FilePlan:
    row_groups, pruned = _row_groups(meta.row_groups, request.symbols)
    return FilePlan(
        meta.path,
        meta.day,
        projected,
        row_groups,
        pruned,
        tuple(defect.code.value for defect in ledger.for_day(meta.day, request.stream)),
        len(meta.row_groups),
        sum(rg.byte_size for rg in meta.row_groups),
    )


def _row_groups(
    row_groups: tuple[RowGroupMeta, ...],
    symbols: frozenset[str] | None,
) -> tuple[tuple[RowGroupMeta, ...], bool]:
    if symbols is None:
        return row_groups, False
    return tuple(rg for rg in row_groups if _overlaps(rg, symbols)), True


def _overlaps(row_group: RowGroupMeta, symbols: frozenset[str]) -> bool:
    return any(row_group.symbol_min <= symbol <= row_group.symbol_max for symbol in symbols)
