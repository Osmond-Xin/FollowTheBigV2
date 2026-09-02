"""RawStore 的契约：通过接口观察行为（catalog → plan → execute），不触碰内部。"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from ftbv2.core.raw.plan import plan
from ftbv2.core.raw.types import CONTINUOUS, Gap, GapReason, Quality, ReadRequest, Window
from ftbv2.io.raw.store import RawStore
from tests.raw.conftest import DAY, DAY6, DAY_RESCUE, order_row, trade_row, write_preserve

STRINGISH = (pl.String, pl.Categorical)


def read(store, ledger, **kw):
    req = ReadRequest(**{"stream": "orders", "days": (DAY,), "fields": ("time_ms", "oid", "side", "price", "vol"), **kw})
    cat = store.catalog(req.stream, req.days)
    return store.execute(plan(req, cat, ledger))


def test_store_fails_loud_when_root_missing(tmp_path, ledger):
    with pytest.raises(RuntimeError):
        RawStore(tmp_path / "not-mounted", ledger)


def test_store_fails_loud_when_stream_dir_missing(root, ledger):
    (root / "xinqing").rmdir()
    with pytest.raises(RuntimeError):
        RawStore(root, ledger)


def test_execute_semantic_names_dtypes_and_values(root, ledger):
    write_preserve(root, "orders", DAY, [order_row("000001.SZ", "093000500", oid="7", side="S", price="123400", vol="200")])
    res = read(RawStore(root, ledger), ledger)
    f = res.frame
    assert f.columns == ["symbol", "time_ms", "oid", "side", "price", "vol"]
    assert f.schema["time_ms"] == pl.Int64 and f.schema["oid"] == pl.Int64
    assert f.schema["price"] == pl.Int64 and f.schema["vol"] == pl.Int64
    assert f.schema["symbol"] in STRINGISH and f.schema["side"] in STRINGISH
    assert f.row(0) == ("000001.SZ", (9 * 3600 + 30 * 60) * 1000 + 500, 7, "S", 123400, 200)
    assert res.stats.rows == 1 and res.gaps == ()


def test_execute_int64_edge_table(root, ledger):
    raw = ["", " ", "\x00", "18446744073709551615", "1.2018e+006", "abc", "2151938037"]
    rows = [order_row("000001.SZ", "093000000", oid=str(i), vol=v) for i, v in enumerate(raw)]
    write_preserve(root, "orders", DAY, rows)
    res = read(RawStore(root, ledger), ledger, fields=("oid", "vol"))
    assert res.frame.sort("oid")["vol"].to_list() == [None, None, None, None, 1201800, None, 2151938037]


def test_execute_preserves_file_order(root, ledger):
    rows = [order_row("000001.SZ", t, oid=str(i)) for i, t in enumerate(["093000000", "093000000", "092959000", "093001000"])]
    write_preserve(root, "orders", DAY, rows)
    res = read(RawStore(root, ledger), ledger, fields=("oid",))
    assert res.frame["oid"].to_list() == [0, 1, 2, 3]


def test_execute_enum_unknown_values_preserved(root, ledger):
    rows = [order_row("000001.SZ", "093000000", oid=str(i), side=s) for i, s in enumerate(["B", " ", "C", "I"])]
    write_preserve(root, "orders", DAY, rows)
    res = read(RawStore(root, ledger), ledger, fields=("oid", "side"))
    assert res.frame.sort("oid")["side"].cast(pl.String).to_list() == ["B", " ", "C", "I"]


def test_execute_nul_sentinel_kept_in_string_and_null_in_int(root, ledger):
    write_preserve(root, "trades", DAY, [trade_row("600000.SH", "093000000", seq="\x00", code="\x00")])
    res = read(RawStore(root, ledger), ledger, stream="trades", fields=("seq", "code"))
    assert res.frame["code"].cast(pl.String).to_list() == ["\x00"]
    assert res.frame["seq"].to_list() == [None]


def test_execute_single_symbol_reads_subset_of_row_groups(root, ledger):
    syms = ["000001.SZ", "000002.SZ", "600000.SH", "600001.SH"]
    rows = [order_row(s, "093000000", oid=str(k)) for s in syms for k in range(50)]
    write_preserve(root, "orders", DAY, rows, row_group_rows=50)          # 4 个 row group，每标的一个
    res = read(RawStore(root, ledger), ledger, fields=("oid",), symbols=frozenset({"600000.SH"}))
    assert res.stats.row_groups_total == 4 and res.stats.row_groups_read == 1
    assert res.stats.bytes_read < res.stats.bytes_total
    assert res.frame["symbol"].cast(pl.String).unique().to_list() == ["600000.SH"] and res.stats.rows == 50


def test_execute_symbol_exact_after_statistics_pruning(root, ledger):
    rows = [order_row(s, "093000000") for s in ["000001.SZ", "000005.SZ", "000009.SZ"]]
    write_preserve(root, "orders", DAY, rows)                               # 一个 row group 覆盖三只
    res = read(RawStore(root, ledger), ledger, fields=("oid",), symbols=frozenset({"000005.SZ"}))
    assert res.frame["symbol"].cast(pl.String).to_list() == ["000005.SZ"]


def test_execute_window_continuous_session(root, ledger):
    times = {"091500000": "pre", "093100000": "am", "114500000": "lunch", "130500000": "pm", "145800000": "close"}
    rows = [order_row("000001.SZ", t, oid=str(i)) for i, t in enumerate(times)]
    write_preserve(root, "orders", DAY, rows)
    res = read(RawStore(root, ledger), ledger, fields=("oid",), windows=CONTINUOUS)
    assert res.frame["oid"].to_list() == [1, 3]
    assert res.frame.columns == ["symbol", "oid"]                            # 过滤所需的 time 列不泄漏到输出


def test_execute_custom_window_half_open(root, ledger):
    rows = [order_row("000001.SZ", t, oid=str(i)) for i, t in enumerate(["093000000", "093000001", "093100000"])]
    write_preserve(root, "orders", DAY, rows)
    res = read(RawStore(root, ledger), ledger, fields=("oid",), windows=(Window(34_200_000, 34_260_000),))
    assert res.frame["oid"].to_list() == [0, 1]


def test_execute_six_digit_time_unregistered_hard_fails(root, ledger):
    write_preserve(root, "orders", DAY, [order_row("600000.SH", "84500"), order_row("600000.SH", "093000000")])
    with pytest.raises(RuntimeError, match="time_6digit"):
        read(RawStore(root, ledger), ledger, fields=("time_ms",))


def test_execute_six_digit_time_registered_normalized(root, ledger):
    rows = [order_row("600000.SH", "84500", oid="0"), order_row("600000.SH", "093000500", oid="1")]
    write_preserve(root, "orders", DAY6, rows)
    res = read(RawStore(root, ledger), ledger, days=(DAY6,), fields=("oid", "time_ms"))
    assert res.frame.sort("oid")["time_ms"].to_list() == [(8 * 3600 + 45 * 60) * 1000, (9 * 3600 + 30 * 60) * 1000 + 500]


def test_execute_gap_symbol_absent(root, ledger):
    write_preserve(root, "orders", DAY, [order_row("000001.SZ", "093000000")])
    res = read(RawStore(root, ledger), ledger, fields=("oid",), symbols=frozenset({"000001.SZ", "000002.SZ"}))
    assert res.gaps == (Gap(DAY, "orders", GapReason.SYMBOL_ABSENT, "000002.SZ"),)
    assert res.frame.height == 1


def test_execute_gap_stream_partial_on_rescue_day(root, ledger):
    write_preserve(root, "orders", DAY_RESCUE, [order_row("000001.SZ", "093000000")])
    write_preserve(root, "trades", DAY_RESCUE, [trade_row("000001.SZ", "093000000"), trade_row("000002.SZ", "093000000")])
    res = read(RawStore(root, ledger), ledger, days=(DAY_RESCUE,), fields=("oid",), symbols=frozenset({"000002.SZ"}))
    assert [g.reason for g in res.gaps] == [GapReason.STREAM_PARTIAL]


def test_execute_gap_day_missing(root, ledger):
    d2 = dt.date(2022, 1, 5)
    write_preserve(root, "orders", DAY, [order_row("000001.SZ", "093000000")])
    res = read(RawStore(root, ledger), ledger, days=(DAY, d2), fields=("oid",))
    assert [(g.day, g.reason) for g in res.gaps] == [(d2, GapReason.DAY_MISSING)] and res.frame.height == 1


def test_execute_raw_passthrough_is_string(root, ledger):
    write_preserve(root, "orders", DAY, [order_row("000001.SZ", "093000000")])
    res = read(RawStore(root, ledger), ledger, fields=("raw:column_3",))
    assert res.frame.columns == ["symbol", "raw:column_3"] and res.frame["raw:column_3"].to_list() == ["20220104"]


def test_days_requires_all_three_streams_and_sorts(root, ledger):
    d2 = dt.date(2022, 1, 5)
    for s in ("orders", "trades", "xinqing"):
        write_preserve(root, s, d2, [])
    write_preserve(root, "orders", DAY, [])
    write_preserve(root, "trades", DAY, [])
    assert RawStore(root, ledger).days() == (d2,)


def test_quality_defaults_unverified(root, ledger):
    write_preserve(root, "orders", DAY, [])
    assert RawStore(root, ledger).quality(DAY) is Quality.UNVERIFIED
