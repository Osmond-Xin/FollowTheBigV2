"""plan() 的契约：纯函数，CI 不碰数据即可断言。目录元数据手工构造。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from ftbv2.core.raw.ledger import parse_ledger
from ftbv2.core.raw.plan import plan
from ftbv2.core.raw.types import Catalog, FileMeta, ReadRequest, RowGroupMeta, Window
from tests.raw.conftest import DAY, DAY6, TIME6, ledger_toml

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


def test_plan_single_pass_and_ledger_binding(ledger):
    p = plan(request(days=(DAY, dt.date(2022, 1, 5))), catalog(DAY, dt.date(2022, 1, 5)), ledger)
    assert len({f.path for f in p.files}) == len(p.files) == 2
    assert p.ledger_sha256 == ledger.sha256


def test_plan_prunes_row_groups_by_symbol_statistics(ledger):
    p = plan(request(symbols=frozenset({"600001.SH"})), catalog(DAY), ledger)
    assert [rg.index for rg in p.files[0].row_groups] == [2] and p.files[0].pruned
    p2 = plan(request(symbols=frozenset({"000005.SZ", "605000.SH"})), catalog(DAY), ledger)
    assert [rg.index for rg in p2.files[0].row_groups] == [0, 2]
    assert "symbol_exact" in p2.post_filters


def test_plan_symbol_on_row_group_boundary_and_in_gap(ledger):
    edge = plan(request(symbols=frozenset({"000010.SZ", "600001.SH"})), catalog(DAY), ledger)
    assert [rg.index for rg in edge.files[0].row_groups] == [0, 2]          # min/max 本身要命中
    gap = plan(request(symbols=frozenset({"000010.SZ", "000011.SZ"})), catalog(DAY), ledger)
    assert [rg.index for rg in gap.files[0].row_groups] == [0, 1]          # 区间是闭区间，不多读第 2 组


def test_plan_without_symbols_reads_all_row_groups(ledger):
    p = plan(request(), catalog(DAY), ledger)
    f = p.files[0]
    assert f.row_groups == RG and not f.pruned and f.total_row_groups == 3 and f.total_bytes == 3000
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
    assert q.output_fields == ("day", "symbol", "price", "vol")


def test_plan_output_fields_reserved_first_and_dedup(ledger):
    p = plan(request(fields=("vol", "symbol", "vol", "day", "time_ms")), catalog(DAY), ledger)
    assert p.output_fields == ("day", "symbol", "vol", "time_ms")


def test_plan_follows_request_day_order_and_skips_missing(ledger):
    d2 = dt.date(2022, 1, 5)
    p = plan(request(days=(d2, DAY)), catalog(DAY, d2), ledger)
    assert tuple(f.day for f in p.files) == (d2, DAY)
    q = plan(request(days=(DAY, d2)), catalog(d2, missing=(DAY,)), ledger)
    assert tuple(f.day for f in q.files) == (d2,)


def test_plan_patches_are_per_file_from_ledger(ledger):
    p = plan(request(days=(DAY, DAY6)), catalog(DAY, DAY6), ledger)
    by_day = {f.day: f.patches for f in p.files}
    assert "time_6digit" in by_day[DAY6] and "time_6digit" not in by_day[DAY]


def test_plan_patch_respects_ledger_stream_scope():
    only_trades = parse_ledger(ledger_toml(TIME6 + '\nstream = "trades"'))
    p = plan(request(days=(DAY6,)), catalog(DAY6), only_trades)
    assert "time_6digit" not in p.files[0].patches


def test_plan_unknown_field_raises(ledger):
    with pytest.raises(KeyError):
        plan(request(fields=("nonexistent_x",)), catalog(DAY), ledger)
    with pytest.raises(KeyError):
        plan(request(fields=("raw:column_5",)), catalog(DAY), ledger)      # 旁路不在读取路径上


def test_request_validation():
    with pytest.raises(ValueError):
        Window(10, 10)
    with pytest.raises(ValueError):
        Window(0, 86_400_001)
    with pytest.raises(ValueError):
        request(symbols=frozenset())
    with pytest.raises(ValueError):
        request(symbols=frozenset({"600000"}))
    with pytest.raises(ValueError):
        request(fields=())
    with pytest.raises(ValueError):
        request(windows=())
