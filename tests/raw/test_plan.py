"""plan() 的契约：纯函数，CI 不碰数据即可断言。目录元数据手工构造。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from ftbv2.core.raw.plan import plan
from ftbv2.core.raw.types import Catalog, FileMeta, ReadRequest, RowGroupMeta, Window
from tests.raw.conftest import DAY, DAY6

RG = (
    RowGroupMeta(0, 100, 1000, "000001.SZ", "000010.SZ"),
    RowGroupMeta(1, 100, 1000, "000011.SZ", "600000.SH"),
    RowGroupMeta(2, 100, 1000, "600001.SH", "605599.SH"),
)
ORDER_COLS = tuple(f"column_{i}" for i in range(1, 12)) + ("_symbol",)


def catalog(*days: dt.date, missing: tuple[dt.date, ...] = ()) -> Catalog:
    files = tuple(FileMeta(Path(f"/x/orders/date={d:%Y%m%d}.parquet"), "orders", d, 300, ORDER_COLS, RG) for d in days)
    return Catalog("orders", files, missing)


def request(**kw) -> ReadRequest:
    base = dict(stream="orders", days=(DAY,), fields=("price", "vol"))
    base.update(kw)
    return ReadRequest(**base)


def test_plan_is_single_pass_prebuffer_pyarrow(ledger):
    p = plan(request(), catalog(DAY), ledger)
    assert p.passes == 1 and p.pre_buffer is True and p.engine == "pyarrow"


def test_plan_prunes_row_groups_by_symbol_statistics(ledger):
    p = plan(request(symbols=frozenset({"600001.SH"})), catalog(DAY), ledger)
    assert p.files[0].row_groups == (2,)
    p2 = plan(request(symbols=frozenset({"000005.SZ", "605000.SH"})), catalog(DAY), ledger)
    assert p2.files[0].row_groups == (0, 2)
    assert "symbol_exact" in p2.post_filters


def test_plan_without_symbols_reads_all_row_groups(ledger):
    p = plan(request(), catalog(DAY), ledger)
    assert p.files[0].row_groups is None
    assert "symbol_exact" not in p.post_filters


def test_plan_window_is_post_filter_and_never_prunes(ledger):
    w = (Window(34_200_000, 41_400_000),)
    with_sym = plan(request(symbols=frozenset({"600001.SH"}), windows=w), catalog(DAY), ledger)
    without_w = plan(request(symbols=frozenset({"600001.SH"})), catalog(DAY), ledger)
    assert "window" in with_sym.post_filters
    assert with_sym.files[0].row_groups == without_w.files[0].row_groups


def test_plan_projection_minimal_and_expanded_for_filters(ledger):
    p = plan(request(), catalog(DAY), ledger)
    assert set(p.files[0].columns) == {"_symbol", "column_9", "column_10"}
    q = plan(request(symbols=frozenset({"600001.SH"}), windows=(Window(0, 1),)), catalog(DAY), ledger)
    assert set(q.files[0].columns) == {"_symbol", "column_4", "column_9", "column_10"}
    assert q.output_fields == ("symbol", "price", "vol")


def test_plan_output_fields_symbol_first_dedup(ledger):
    p = plan(request(fields=("vol", "symbol", "vol", "time_ms")), catalog(DAY), ledger)
    assert p.output_fields == ("symbol", "vol", "time_ms")


def test_plan_skips_missing_days_and_keeps_day_order(ledger):
    d2 = dt.date(2022, 1, 5)
    p = plan(request(days=(DAY, d2)), catalog(d2, missing=(DAY,)), ledger)
    assert tuple(f.day for f in p.files) == (d2,)


def test_plan_patch_from_ledger(ledger):
    p6 = plan(request(days=(DAY6,)), catalog(DAY6), ledger)
    p = plan(request(), catalog(DAY), ledger)
    assert "time_6digit" in p6.patches and "time_6digit" not in p.patches


def test_plan_unknown_field_raises(ledger):
    with pytest.raises(KeyError):
        plan(request(fields=("feature_x",)), catalog(DAY), ledger)


def test_plan_raw_passthrough_field(ledger):
    p = plan(request(fields=("raw:column_5",)), catalog(DAY), ledger)
    assert "column_5" in p.files[0].columns and p.output_fields == ("symbol", "raw:column_5")
