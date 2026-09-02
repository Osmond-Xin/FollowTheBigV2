"""ingest() 的契约：7z → preserve。用 7zz 现打一个三标的、三 stream 的小归档，走完整链路再用 RawStore 读回。"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq
import pytest

from ftbv2.core.raw.plan import plan
from ftbv2.core.raw.schema import CSV_NAME, ROW_GROUP_ROWS
from ftbv2.core.raw.types import GapReason, ReadRequest
from ftbv2.io.raw.ingest import ingest
from ftbv2.io.raw.store import RawStore

pytestmark = pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")

DAY = dt.date(2022, 1, 4)
HEADER = {
    "orders": "万得代码,交易所代码,自然日,时间,委托编号,交易所委托号,委托类型,委托代码,委托价格,委托数量,",
    "trades": "万得代码,交易所代码,自然日,时间,成交编号,成交代码,委托代码,BS标志,成交价格,成交数量,叫卖序号,叫买序号,",
    "xinqing": "万得代码,交易所代码,自然日,时间," + ",".join(f"f{i}" for i in range(5, 68)),
}


def _csv_rows(stream: str, symbol: str, n: int) -> list[str]:
    ncol = {"orders": 11, "trades": 13, "xinqing": 67}[stream]
    rows = []
    for k in range(n):
        vals = [symbol, symbol[:6], "20220104", f"0930{k:02d}000"] + [str(100 + k)] * (ncol - 4)
        if stream == "trades" and symbol.endswith(".SH"):
            vals[5] = "\x00"                                   # SH 成交代码是 NUL 字节，必须原样保留
        rows.append(",".join(vals))
    return rows


def make_archive(tmp_path: Path, symbols: dict[str, int], only: dict[str, tuple[str, ...]] | None = None) -> Path:
    """symbols: 标的 → 每个 stream 的行数。布局 {day}/{symbol}/{csv}，表头 GBK。
    only: 标的 → 只写这些 stream 的 CSV（造停牌心跳 / 残缺归档）。"""
    src = tmp_path / "src" / f"{DAY:%Y%m%d}"
    for sym, n in symbols.items():
        d = src / sym
        d.mkdir(parents=True)
        for stream, name in CSV_NAME.items():
            if only and sym in only and stream not in only[sym]:
                continue
            body = "\n".join([HEADER[stream], *_csv_rows(stream, sym, n)]) + "\n"
            (d / name).write_bytes(body.encode("gbk"))
    archive = tmp_path / f"{DAY:%Y%m%d}.7z"
    subprocess.run(["7zz", "a", "-bso0", "-bsp0", str(archive), f"{DAY:%Y%m%d}"], cwd=src.parent, check=True)
    return archive


@pytest.fixture
def archive(tmp_path):
    return make_archive(tmp_path, {"600000.SH": 3, "000001.SZ": 2, "300001.SZ": 4, "000002.SZ": 1})


def test_ingest_writes_preserve_layout_and_receipt(archive, root, ledger):
    r = ingest(DAY, archive, root)
    assert r.day == DAY and {s.stream for s in r.streams} == {"orders", "trades", "xinqing"}
    assert r.dropped_by_prefix == {"300": 1} and r.prefixes == ("000", "001", "002", "003", "600", "601", "603", "605")
    assert len(r.archive_sha256) == 64 and r.sevenzip_version
    for s in r.streams:
        assert s.n_symbols == 3 and s.n_rows_csv == s.n_rows_parquet == 6
        assert s.header == HEADER[s.stream]
        meta = pq.read_metadata(root / s.stream / f"date={DAY:%Y%m%d}.parquet")
        assert meta.num_rows == 6
    assert (root / "manifest" / f"{DAY:%Y%m%d}.json").exists()


def test_ingest_columns_are_column_n_large_string_plus_symbol(archive, root, ledger):
    ingest(DAY, archive, root)
    schema = pq.read_schema(root / "orders" / f"date={DAY:%Y%m%d}.parquet")
    assert schema.names == [f"column_{i}" for i in range(1, 12)] + ["_symbol"]
    assert all(str(schema.field(n).type) == "large_string" for n in schema.names)


def test_ingest_sorted_by_symbol_and_row_order_kept(archive, root, ledger):
    ingest(DAY, archive, root)
    t = pl.read_parquet(root / "orders" / f"date={DAY:%Y%m%d}.parquet", columns=["_symbol", "column_4"])
    assert t["_symbol"].to_list() == ["000001.SZ"] * 2 + ["000002.SZ"] + ["600000.SH"] * 3
    assert t.filter(pl.col("_symbol") == "600000.SH")["column_4"].to_list() == ["093000000", "093001000", "093002000"]


def test_ingest_preserves_nul_byte_and_readable_by_store(archive, root, ledger):
    ingest(DAY, archive, root)
    store = RawStore(root, ledger)
    req = ReadRequest("trades", (DAY,), ("code", "seq"), symbols=frozenset({"600000.SH"}))
    res = store.execute(plan(req, store.catalog("trades", (DAY,)), ledger))
    assert res.frame["code"].cast(pl.String).unique().to_list() == ["\x00"]
    assert res.frame["seq"].to_list() == [100, 101, 102]


def test_ingest_row_group_size_matches_existing_files(tmp_path, root, ledger):
    big = make_archive(tmp_path, {"000001.SZ": ROW_GROUP_ROWS + 10})
    ingest(DAY, big, root)
    meta = pq.read_metadata(root / "orders" / f"date={DAY:%Y%m%d}.parquet")
    assert meta.num_row_groups == 2 and meta.row_group(0).num_rows == ROW_GROUP_ROWS


def test_ingest_trailing_blank_lines_do_not_count(tmp_path, root, ledger):
    src = tmp_path / "src" / f"{DAY:%Y%m%d}" / "000001.SZ"
    src.mkdir(parents=True)
    for stream, name in CSV_NAME.items():
        body = "\r\n".join([HEADER[stream], *_csv_rows(stream, "000001.SZ", 2)]) + "\r\n\r\n\r\n"
        (src / name).write_bytes(body.encode("gbk"))
    archive = tmp_path / "t.7z"
    subprocess.run(["7zz", "a", "-bso0", "-bsp0", str(archive), f"{DAY:%Y%m%d}"], cwd=src.parent.parent, check=True)
    r = ingest(DAY, archive, root)
    assert all(s.n_rows_csv == s.n_rows_parquet == 2 for s in r.streams)


def test_ingest_rejects_changed_archive_for_same_day(tmp_path, root, ledger):
    a1 = make_archive(tmp_path / "a", {"000001.SZ": 1})
    a2 = make_archive(tmp_path / "b", {"000001.SZ": 2})
    ingest(DAY, a1, root)
    with pytest.raises(RuntimeError, match="sha256|归档"):
        ingest(DAY, a2, root)


def test_ingest_rejects_changed_prefixes_for_same_day(tmp_path, root, ledger):
    a = make_archive(tmp_path, {"000001.SZ": 1, "300001.SZ": 1})
    ingest(DAY, a, root)
    with pytest.raises(RuntimeError, match="prefix|前缀"):
        ingest(DAY, a, root, prefixes=("000", "300"))


def test_ingest_rejects_path_traversal_entries(tmp_path, root, ledger):
    """归档里带 ../ 的条目必须整体拒绝，root 不得有任何改动。"""
    evil = tmp_path / "evil"
    (evil / "x").mkdir(parents=True)
    (evil / "x" / "manifest.json").write_text("{}")
    archive = tmp_path / "evil.7z"
    subprocess.run(["7zz", "a", "-bso0", "-bsp0", "-spf2", str(archive), "x/../../evil/x/manifest.json"], cwd=evil, check=True)
    names = subprocess.run(["7zz", "l", "-slt", "-ba", str(archive)], capture_output=True, text=True, check=True).stdout
    if ".." not in names:
        pytest.skip("本机 7zz 会规范化路径，造不出穿越条目")
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    with pytest.raises(RuntimeError):
        ingest(DAY, archive, root)
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before


def test_ingest_is_idempotent_and_atomic(archive, root, ledger):
    r1 = ingest(DAY, archive, root)
    f = root / "orders" / f"date={DAY:%Y%m%d}.parquet"
    mtime = f.stat().st_mtime_ns
    r2 = ingest(DAY, archive, root)
    assert r2 == r1 and f.stat().st_mtime_ns == mtime
    assert not list(root.rglob("*.tmp")) and not list(root.rglob("*.partial"))
    assert RawStore(root, ledger).days() == (DAY,)


# ----------------------------------------------------------------- 停牌心跳（数据表第四节，2026-09-02 实测裁定）


def test_ingest_quote_only_symbol_is_legal_and_recorded(tmp_path, root, ledger):
    """标的目录只有行情.csv（停牌心跳）：合法，只进 xinqing，记入 quote_only_symbols；三 stream 的 n_symbols 因此不等；
    manifest 往返后幂等返回的 receipt 带同样的字段。"""
    archive = make_archive(tmp_path, {"600000.SH": 3, "000001.SZ": 2, "000780.SZ": 5}, only={"000780.SZ": ("xinqing",)})
    r = ingest(DAY, archive, root)
    assert r.quote_only_symbols == ("000780.SZ",)
    by_stream = {s.stream: s for s in r.streams}
    assert by_stream["xinqing"].n_symbols == 3 and by_stream["xinqing"].n_rows_csv == 10
    assert by_stream["orders"].n_symbols == by_stream["trades"].n_symbols == 2
    assert by_stream["orders"].n_rows_csv == by_stream["trades"].n_rows_csv == 5
    manifest = json.loads((root / "manifest" / f"{DAY:%Y%m%d}.json").read_text(encoding="utf-8"))
    assert manifest["quote_only_symbols"] == ["000780.SZ"]
    assert ingest(DAY, archive, root) == r                      # 幂等路径从 manifest 重建，字段不丢


def test_ingest_quote_only_symbol_reads_back_as_symbol_absent_gap(tmp_path, root, ledger):
    """读取侧不变：请求停牌标的的 orders ⇒ SYMBOL_ABSENT 缺口，xinqing 里有它的行。"""
    archive = make_archive(tmp_path, {"600000.SH": 3, "000780.SZ": 4}, only={"000780.SZ": ("xinqing",)})
    ingest(DAY, archive, root)
    store = RawStore(root, ledger)
    req = ReadRequest("orders", (DAY,), ("price",), symbols=frozenset({"000780.SZ"}))
    res = store.execute(plan(req, store.catalog("orders", (DAY,)), ledger))
    assert res.frame.height == 0
    assert [(g.reason, g.symbol) for g in res.gaps] == [(GapReason.SYMBOL_ABSENT, "000780.SZ")]
    req = ReadRequest("xinqing", (DAY,), ("last_price",), symbols=frozenset({"000780.SZ"}))
    res = store.execute(plan(req, store.catalog("xinqing", (DAY,)), ledger))
    assert res.frame.height == 4 and res.gaps == ()


def test_ingest_quote_only_symbol_outside_prefixes_is_dropped_not_recorded(tmp_path, root, ledger):
    """前缀筛掉的停牌标的走 dropped_by_prefix，不进 quote_only_symbols。"""
    archive = make_archive(tmp_path, {"600000.SH": 3, "300261.SZ": 4}, only={"300261.SZ": ("xinqing",)})
    r = ingest(DAY, archive, root)
    assert r.quote_only_symbols == () and r.dropped_by_prefix == {"300": 1}


@pytest.mark.parametrize("present", [("orders", "trades"), ("xinqing", "trades"), ("orders",)])
def test_ingest_other_partial_symbol_dirs_are_still_truncated_archives(tmp_path, root, ledger, present):
    """缺行情、或只缺委托 / 成交之一、或只有委托：仍是残缺归档 ⇒ RuntimeError，不写 manifest。"""
    archive = make_archive(tmp_path, {"600000.SH": 3, "000001.SZ": 2}, only={"000001.SZ": present})
    with pytest.raises(RuntimeError, match="缺 stream CSV"):
        ingest(DAY, archive, root)
    assert not (root / "manifest" / f"{DAY:%Y%m%d}.json").exists()
