"""合成 preserve 夹具：与真实文件同形（column_N 全 large_string、_symbol 末列、按 _symbol 排序、多 row group）。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ftbv2.core.raw.ledger import DefectLedger, parse_ledger
from ftbv2.core.raw.schema import STREAMS

NCOLS = {"orders": 11, "trades": 13, "xinqing": 67}
DAY = dt.date(2022, 1, 4)
DAY6 = dt.date(2024, 2, 6)          # 账本登记了 time_6digit 的天
DAY_RESCUE = dt.date(2026, 8, 5)    # 账本登记了 rescue_partial 的天

_DEFAULTS = {"kind": '"defect"', "status": '"active"', "created_at": "2026-09-01",
             "evidence": '"夹具"', "evidence_sha256": '"' + "0" * 64 + '"', "read_layer_action": '"gap"'}


def ledger_toml(*entries: str) -> str:
    """账本工厂：每个测试自己声明用哪几条，不共享一份写死的账本。
    条目只需写 code / days / stream 等关心的字段，其余必填字段（kind / status / created_at / evidence /
    read_layer_action）按默认补齐；time_6digit 默认 action = patch。"""
    out = []
    for i, e in enumerate(entries, 1):
        given = {line.split("=")[0].strip() for line in e.splitlines() if "=" in line}
        defaults = dict(_DEFAULTS)
        if 'code = "time_6digit"' in e:
            defaults["read_layer_action"] = '"patch"'
        elif "days" not in given:
            defaults["read_layer_action"] = '"none"'          # 结构性条目不按天归因
        extra = "".join(f"{k} = {v}\n" for k, v in defaults.items() if k not in given)
        out.append(f"[[defect]]\nid = \"D{i:03d}\"\n{e}\n{extra}\n")
    return "".join(out)


TIME6 = 'code = "time_6digit"\ndays = [2024-02-06]'
RESCUE = 'code = "rescue_partial"\ndays = [2026-08-05]'
NUL = 'code = "nul_sentinel_sh"\nstream = "trades"'


def order_row(symbol: str, time: str, oid: str = "1", typ: str = "0", side: str = "B",
              price: str = "100000", vol: str = "100") -> dict[str, str]:
    return {"column_1": symbol, "column_2": symbol[:6], "column_3": "20220104", "column_4": time,
            "column_5": "", "column_6": oid, "column_7": typ, "column_8": side,
            "column_9": price, "column_10": vol, "column_11": "", "_symbol": symbol}


def trade_row(symbol: str, time: str, seq: str = "1", code: str = "0", bs: str = "B",
              price: str = "100000", vol: str = "100", ask_ref: str = "0", bid_ref: str = "0") -> dict[str, str]:
    return {"column_1": symbol, "column_2": symbol[:6], "column_3": "20220104", "column_4": time,
            "column_5": seq, "column_6": code, "column_7": "0", "column_8": bs, "column_9": price,
            "column_10": vol, "column_11": ask_ref, "column_12": bid_ref, "column_13": "", "_symbol": symbol}


def write_preserve(root: Path, stream: str, day: dt.date, rows: list[dict[str, str]],
                   row_group_rows: int | None = None) -> Path:
    """rows 按 _symbol 稳定排序后写入；row_group_rows 控制 row group 切分，便于断言裁剪。"""
    rows = sorted(rows, key=lambda r: r["_symbol"])
    cols = [f"column_{i}" for i in range(1, NCOLS[stream] + 1)] + ["_symbol"]
    table = pa.table({c: pa.array([r.get(c, "") for r in rows], pa.large_string()) for c in cols})
    path = root / stream / f"date={day:%Y%m%d}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, row_group_size=row_group_rows or max(len(rows), 1), compression="zstd")
    return path


@pytest.fixture
def ledger() -> DefectLedger:
    return parse_ledger(ledger_toml(TIME6, RESCUE, NUL))


@pytest.fixture
def empty_ledger() -> DefectLedger:
    return parse_ledger(ledger_toml())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "preserve"
    for s in STREAMS:
        (r / s).mkdir(parents=True)
    (r / "manifest").mkdir()
    return r
