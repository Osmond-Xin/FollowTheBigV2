"""审计模块与批量驱动的契约：比对口径、形状扫描、跨流集合差、读取计时；archive_day；ingest_days 的登记与磁盘下限。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from ftbv2.core.raw.schema import archive_day
from ftbv2.io.raw.audit import compare_preserve, preserve_days, read_floor, scan_time_shapes, symbol_mismatches
from ftbv2.io.raw.ingest import ingest_days
from ftbv2.io.raw.store import RawStore
from tests.raw.conftest import DAY, order_row, trade_row, write_preserve


def _root(tmp_path: Path, name: str, rows_orders: list[dict], symbols_xq: tuple[str, ...] = ("000001.SZ",)) -> Path:
    root = tmp_path / name
    write_preserve(root, "orders", DAY, rows_orders)
    write_preserve(root, "trades", DAY, [trade_row("000001.SZ", "093000000")])
    write_preserve(root, "xinqing", DAY, [{"column_4": "93000000", "_symbol": s} for s in symbols_xq])
    return root


def test_archive_day_accepts_only_canonical_names():
    assert archive_day("20220104.7z") == dt.date(2022, 1, 4)
    for bad in ("20220104(1).7z", "20220104.7z.baiduyun.p.downloading", "2022-01-04.7z", "20221301.7z", "notes.txt"):
        assert archive_day(bad) is None, bad


def test_compare_preserve_reports_null_as_empty_and_real_differences(tmp_path):
    a = _root(tmp_path, "a", [order_row("000001.SZ", "093000000", side="B"), order_row("000002.SZ", "093001000")])
    b = _root(tmp_path, "b", [order_row("000001.SZ", "093000000", side="B"), order_row("000002.SZ", "093001000")])
    result = {r.stream: r for r in compare_preserve(a, b, DAY)}
    assert all(r.identical_modulo_null for r in result.values())
    c = _root(tmp_path, "c", [order_row("000001.SZ", "093000000", side="S"), order_row("000002.SZ", "093001000")])
    diff = {r.stream: r for r in compare_preserve(a, c, DAY)}
    assert diff["orders"].cells_differ == {"column_8": 1} and not diff["orders"].identical_modulo_null
    d = _root(tmp_path, "d", [order_row("000001.SZ", "093000000")])
    short = {r.stream: r for r in compare_preserve(a, d, DAY)}
    assert short["orders"].rows == (2, 1) and not short["orders"].identical_modulo_null


def test_scan_time_shapes_and_symbol_mismatches(tmp_path):
    root = _root(tmp_path, "r", [order_row("000001.SZ", "93000000"), order_row("000001.SZ", "093000000")], symbols_xq=("000001.SZ", "000780.SZ"))
    obs = {(o.day, o.stream): o.lengths for o in scan_time_shapes(root, [DAY])}
    assert obs[(DAY, "orders")] == {8: 1, 9: 1} and obs[(DAY, "xinqing")] == {8: 2}
    (mm,) = symbol_mismatches(root, [DAY])
    assert mm.only_in == {"xinqing": ("000780.SZ",)}
    assert preserve_days(root) == (DAY,)


def test_read_floor_times_every_stream(tmp_path, ledger):
    root = _root(tmp_path, "r", [order_row("000001.SZ", "093000000")])
    (root / "manifest").mkdir()
    timings = read_floor(RawStore(root, ledger), ledger, [DAY])
    assert [t.stream for t in timings] == ["orders", "trades", "xinqing"] and all(t.rows >= 1 for t in timings)


def test_ingest_days_registers_noncanonical_and_disk_floor(tmp_path):
    root, scratch = tmp_path / "root", tmp_path / "scratch"
    dup = tmp_path / "20220104(1).7z"; dup.write_bytes(b"x")
    result = ingest_days([dup], root, scratch_parent=scratch, min_free_bytes=0, min_free_pct=0.0)
    assert result.skipped == ((dup, "非规范文件名（重复件 / 半成品 / 非 YYYYMMDD.7z）"),) and not result.ok and result.outcomes == ()
    good = tmp_path / "20220104.7z"; good.write_bytes(b"x")
    result = ingest_days([good], root, scratch_parent=scratch, min_free_bytes=10**18, min_free_pct=0.0)
    (o,) = result.outcomes
    assert o.status == "stopped_disk" and "低于下限" in o.error and not result.ok
    result = ingest_days([good], root, scratch_parent=scratch, min_free_bytes=0, min_free_pct=0.0)
    (o,) = result.outcomes
    assert o.status == "failed" and o.error and not result.ok       # 假归档 ⇒ 7zz 失败被记录，不吞


@pytest.mark.parametrize("stop", [True, False])
def test_ingest_days_stop_policy(tmp_path, stop):
    a, b = tmp_path / "20220104.7z", tmp_path / "20220105.7z"
    a.write_bytes(b"x"); b.write_bytes(b"x")
    result = ingest_days([a, b], tmp_path / "root", scratch_parent=tmp_path / "s", min_free_bytes=0, min_free_pct=0.0, stop_on_error=stop)
    assert len(result.outcomes) == (1 if stop else 2)
