"""三方复审（巩固后）采纳项的回归：每条对应 design-log 2026-09-02-红队-原始层实现-*.md 里的一条。"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ftbv2.core.raw.plan import plan
from ftbv2.core.raw.schema import CSV_NAME
from ftbv2.core.raw.types import Catalog, ReadRequest
from ftbv2.io.raw.ingest import ingest
from ftbv2.io.raw.store import RawStore
from tests.raw.conftest import DAY, order_row, write_preserve
from tests.raw.test_ingest import HEADER, _csv_rows, make_archive

needs_7zz = pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")


def test_plan_rejects_stream_mismatch_and_uncovered_days(root, ledger):
    write_preserve(root, "orders", DAY, [order_row("000001.SZ", "093000000")])
    store = RawStore(root, ledger)
    cat = store.catalog("orders", (DAY,))
    with pytest.raises(ValueError, match="stream"):
        plan(ReadRequest("trades", (DAY,), ("seq",)), cat, ledger)
    with pytest.raises(ValueError, match="未覆盖"):
        plan(ReadRequest("orders", (DAY, dt.date(2022, 1, 5)), ("oid",)), cat, ledger)
    assert plan(ReadRequest("orders", (DAY,), ("oid",)), Catalog("orders", cat.files, ()), ledger).files


def test_short_time_check_ignores_empty_and_seven_digit(root, ledger):
    rows = [order_row("600000.SH", t, oid=str(i)) for i, t in enumerate(["", "0930000", "093000000"])]
    write_preserve(root, "orders", DAY, rows)
    store = RawStore(root, ledger)
    req = ReadRequest("orders", (DAY,), ("oid", "time_ms"))
    res = store.execute(plan(req, store.catalog("orders", (DAY,)), ledger))
    assert res.frame.sort("oid")["time_ms"].to_list() == [None, None, 34_200_000]


def test_catalog_rejects_nonempty_row_group_without_symbol_statistics(root, ledger):
    path = root / "orders" / f"date={DAY:%Y%m%d}.parquet"
    cols = {f"column_{i}": pa.array(["x"], pa.large_string()) for i in range(1, 12)}
    cols["_symbol"] = pa.array(["000001.SZ"], pa.large_string())
    pq.write_table(pa.table(cols), path, write_statistics=False, compression="zstd")
    with pytest.raises(RuntimeError, match="statistics"):
        RawStore(root, ledger).catalog("orders", (DAY,))


def test_to_int64_strips_whitespace_before_precision_check(root, ledger):
    rows = [order_row("000001.SZ", "093000000", oid="0", vol=" 9007199254740993 "), order_row("000001.SZ", "093000000", oid="1", vol=" 42 ")]
    write_preserve(root, "orders", DAY, rows)
    store = RawStore(root, ledger)
    res = store.execute(plan(ReadRequest("orders", (DAY,), ("oid", "vol")), store.catalog("orders", (DAY,)), ledger))
    assert res.frame.sort("oid")["vol"].to_list() == [None, 42]


def _archive_from(tmp_path: Path, files: dict[str, bytes]) -> Path:
    src = tmp_path / "src" / f"{DAY:%Y%m%d}"
    for rel, data in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(data)
    archive = tmp_path / "a.7z"
    subprocess.run(["7zz", "a", "-bso0", "-bsp0", str(archive), f"{DAY:%Y%m%d}"], cwd=src.parent, check=True)
    return archive


def _good_symbol(symbol: str, n: int = 1) -> dict[str, bytes]:
    return {f"{symbol}/{name}": ("\n".join([HEADER[s], *_csv_rows(s, symbol, n)]) + "\n").encode("gbk") for s, name in CSV_NAME.items()}


@needs_7zz
def test_ingest_rejects_bad_symbol_dir_non_csv_file_and_header_drift(tmp_path, root, ledger):
    with pytest.raises(RuntimeError, match="不合法"):
        ingest(DAY, _archive_from(tmp_path / "a", _good_symbol("000BAD.SZ")), root)
    files = _good_symbol("000001.SZ")
    files["000001.SZ/readme.txt"] = b"stray"
    with pytest.raises(RuntimeError, match="未登记"):
        ingest(DAY, _archive_from(tmp_path / "b", files), root)
    drift = {**_good_symbol("000001.SZ"), **_good_symbol("000002.SZ")}
    drift["000002.SZ/逐笔委托.csv"] = drift["000002.SZ/逐笔委托.csv"].replace("委托类型".encode("gbk"), "委托类别".encode("gbk"))
    with pytest.raises(RuntimeError, match="表头不一致"):
        ingest(DAY, _archive_from(tmp_path / "c", drift), root)
    assert not list(root.rglob("*.parquet"))                     # 三次拒绝都没有写出任何产物


@needs_7zz
def test_ingest_rejects_non_ascii_body(tmp_path, root, ledger):
    files = _good_symbol("000001.SZ")
    files["000001.SZ/逐笔委托.csv"] = files["000001.SZ/逐笔委托.csv"] + "000001.SZ,000001,20220104,093001000,,1,0,B,100000,100,乱\n".encode("gbk")
    with pytest.raises(RuntimeError, match="ASCII"):
        ingest(DAY, _archive_from(tmp_path, files), root)


@needs_7zz
def test_ingest_idempotent_return_verifies_parquet_hash(tmp_path, root, ledger):
    archive = make_archive(tmp_path, {"000001.SZ": 2})
    r = ingest(DAY, archive, root)
    assert all(len(s.parquet_sha256) == 64 for s in r.streams)
    target = root / "orders" / f"date={DAY:%Y%m%d}.parquet"
    pl.read_parquet(target).head(1).write_parquet(target)         # 产物被改动
    with pytest.raises(RuntimeError, match="哈希不符"):
        ingest(DAY, archive, root)


@needs_7zz
def test_ingest_day_lock_blocks_concurrent_run(tmp_path, root, ledger):
    archive = make_archive(tmp_path, {"000001.SZ": 1})
    (root / "manifest" / f"{DAY:%Y%m%d}.lock").write_text("")
    with pytest.raises(RuntimeError, match="另一个进程"):
        ingest(DAY, archive, root)
