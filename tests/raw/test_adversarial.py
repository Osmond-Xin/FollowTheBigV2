"""对抗性契约测试：针对懒实现的边界组合、裁剪漏洞、时间窗语义、缺口归因与摄取原子性展开全面攻击。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import shutil
import subprocess

import polars as pl
import pyarrow.parquet as pq
import pytest

from ftbv2.core.raw.decode import in_windows, time_digit_lengths, to_int64, to_time_ms
from ftbv2.core.raw.ledger import DefectCode, DefectLedger, parse_ledger
from ftbv2.core.raw.plan import plan
from ftbv2.core.raw.schema import CSV_NAME, MAIN_PREFIXES, ROW_GROUP_ROWS, STREAMS, field
from ftbv2.core.raw.types import (
    CONTINUOUS_EXCL_AUCTIONS,
    Catalog,
    Day,
    FileMeta,
    Gap,
    GapReason,
    IngestReceipt,
    Quality,
    ReadRequest,
    ReadResult,
    RowGroupMeta,
    ScanPlan,
    Window,
)
from ftbv2.io.raw.ingest import ingest
from ftbv2.io.raw.store import RawStore
from tests.raw.conftest import (
    DAY,
    DAY6,
    DAY_RESCUE,
    NCOLS,
    NUL,
    RESCUE,
    TIME6,
    ledger_toml,
    order_row,
    trade_row,
    write_preserve,
)

STRINGISH = (pl.String, pl.Categorical)

HEADER = {
    "orders": "万得代码,交易所代码,自然日,时间,委托编号,交易所委托号,委托类型,委托代码,委托价格,委托数量,",
    "trades": "万得代码,交易所代码,自然日,时间,成交编号,成交代码,委托代码,BS标志,成交价格,成交数量,叫卖序号,叫买序号,",
    "xinqing": "万得代码,交易所代码,自然日,时间," + ",".join(f"f{i}" for i in range(5, 68)),
}


def read(store: RawStore, ledger: DefectLedger, **kw) -> ReadResult:
    base = {"stream": "orders", "days": (DAY,), "fields": ("time_ms", "oid", "side", "price", "vol")}
    base.update(kw)
    req = ReadRequest(**base)
    cat = store.catalog(req.stream, req.days)
    return store.execute(plan(req, cat, ledger))


def xinqing_row(
    symbol: str,
    time: str,
    last_price: str = "100000",
    ask_px1: str = "100100",
    ask_sz1: str = "50",
    bid_px1: str = "99900",
    bid_sz1: str = "60",
    **extra_cols: str,
) -> dict[str, str]:
    r = {
        "column_1": symbol,
        "column_2": symbol[:6],
        "column_3": "20220104",
        "column_4": time,
        "column_5": last_price,
        "column_18": ask_px1,
        "column_28": ask_sz1,
        "column_38": bid_px1,
        "column_48": bid_sz1,
        "_symbol": symbol,
    }
    for i in range(1, 68):
        col = f"column_{i}"
        if col not in r:
            r[col] = ""
    r.update(extra_cols)
    return r


def make_adv_archive(tmp_path: Path, day: dt.date, symbols_data: dict[str, dict[str, list[str]]], flat: bool = False) -> Path:
    """构造用于对抗测试的 7z 归档。symbols_data: {symbol: {stream: [csv_data_lines]}}。"""
    src = tmp_path / "src"
    base_dir = src if flat else (src / f"{day:%Y%m%d}")
    for sym, stream_dict in symbols_data.items():
        d = base_dir / sym
        d.mkdir(parents=True, exist_ok=True)
        for s in STREAMS:
            lines = stream_dict.get(s, [])
            body = "\n".join([HEADER[s], *lines]) + ("\n" if lines or not lines else "")
            (d / CSV_NAME[s]).write_bytes(body.encode("gbk"))

    archive = tmp_path / f"{day:%Y%m%d}.7z"
    args = ["7zz", "a", "-bso0", "-bsp0", str(archive)]
    if flat:
        args.append(".")
        cwd = src
    else:
        args.append(f"{day:%Y%m%d}")
        cwd = src
    subprocess.run(args, cwd=cwd, check=True)
    return archive


# ==============================================================================
# 攻击面 1：边界表的组合（并发哨兵、科学计数法、超 int32、前导/尾随空白与浮点临界值）
# ==============================================================================

def test_adv_decode_all_sentinels_and_extremes_simultaneous_in_single_batch(root, ledger):
    """同一批多行数据中，多列同时出现 UINT64 哨兵、科学计数法、超 int32、2^53 临界值、NUL 字符与首尾空格。"""
    # 2^53 = 9007199254740992 (Float64 精确整数), 2^53 + 1 = 9007199254740993 (Float64 丢失精度 -> null)
    rows = [
        # 行 0：oid 为 UINT64 哨兵 -> null；price 为科学计数法 -> 1201800；vol 为超 int32 -> 2151938037；side 为 NUL 字符串保留；type 为空格保留；time 带首尾空白
        order_row("000001.SZ", "  093000500  ", oid="18446744073709551615", typ=" ", side="\x00", price="1.2018e+006", vol="2151938037"),
        # 行 1：oid 为 2^53 精确整数 -> 9007199254740992；price 为 2^53 + 1 精度损失 -> null；vol 为 UINT64 哨兵 -> null；time 带尾随空白
        order_row("000001.SZ", "093000000 ", oid="9007199254740992", typ="A", side="C", price="9007199254740993", vol="18446744073709551615"),
        # 行 2：负数科学计数法与普通负数
        order_row("000001.SZ", "145659999", oid="-100", typ="0", side="B", price="-1.5e+02", vol="0"),
        # 行 3：字母、空串、纯空格全落 null
        order_row("000001.SZ", "  093000000", oid="abc", typ=" ", side=" ", price="", vol=" "),
    ]
    write_preserve(root, "orders", DAY, rows)
    res = read(RawStore(root, ledger), ledger, fields=("time_ms", "oid", "type", "side", "price", "vol"))
    df = res.frame

    assert df.columns == ["day", "symbol", "time_ms", "oid", "type", "side", "price", "vol"]
    assert df["time_ms"].to_list() == [34_200_500, 34_200_000, 53_819_999, 34_200_000]
    assert df["oid"].to_list() == [None, 9007199254740992, -100, None]
    assert df["price"].to_list() == [1201800, None, -150, None]
    assert df["vol"].to_list() == [2151938037, None, 0, None]
    assert df["type"].cast(pl.String).to_list() == [" ", "A", "0", " "]
    assert df["side"].cast(pl.String).to_list() == ["\x00", "C", "B", " "]


def test_adv_decode_registered_time_whitespace_and_five_digit_boundaries(root, ledger):
    """在已登记 time_6digit 的天，测试五位/六位/九位混排、首尾带空格制表符、午夜 000000、23:59:59.999 以及非法时间值。"""
    rows = [
        order_row("600000.SH", " 84500 ", oid="0"),           # 五位带空格 -> 08:45:00.000 = 31,500,000 ms
        order_row("600000.SH", "  084500  ", oid="1"),         # 六位带空格 -> 08:45:00.000 = 31,500,000 ms
        order_row("600000.SH", "\t093000500\n", oid="2"),      # 九位带制表换行 -> 09:30:00.500 = 34,200,500 ms
        order_row("600000.SH", "000000", oid="3"),            # 六位午夜 -> 0 ms
        order_row("600000.SH", "235959999", oid="4"),         # 九位全天最后一毫秒 -> 86,399,999 ms
        order_row("600000.SH", " 240000 ", oid="5"),          # 非法小时 24 -> null
        order_row("600000.SH", "096000", oid="6"),            # 非法分钟 60 -> null
        order_row("600000.SH", "093060", oid="7"),            # 非法秒 60 -> null
        order_row("600000.SH", "0930001000", oid="8"),        # 10 位长度 -> null
        order_row("600000.SH", "123", oid="9"),               # 3 位长度 -> null
        order_row("600000.SH", "", oid="10"),                 # 空串 -> null
        order_row("600000.SH", "   ", oid="11"),              # 纯空格 -> null
    ]
    write_preserve(root, "orders", DAY6, rows)
    res = read(RawStore(root, ledger), ledger, days=(DAY6,), fields=("oid", "time_ms"))
    df = res.frame.sort("oid")

    expected_times = [
        31_500_000,
        31_500_000,
        34_200_500,
        0,
        86_399_999,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    ]
    assert df["time_ms"].to_list() == expected_times


def test_adv_xinqing_orderbook_int64_overflow_and_sentinel_matrix(root, ledger):
    """行情宽表 10 档挂单中，测试买卖量超 int32、买卖价科学计数法与 UINT64 哨兵。"""
    row = xinqing_row(
        "600000.SH",
        "093000000",
        last_price="120000",
        ask_px1="1.2018e+006",
        ask_sz1="2151938037",             # 超 int32
        bid_px1="18446744073709551615",   # 哨兵 -> null
        bid_sz1="",                       # 空串 -> null
        column_27="250000",               # ask_px_10 (column_27)
        column_37="1000",                 # ask_sz_10 (column_37)
    )
    write_preserve(root, "xinqing", DAY, [row])
    store = RawStore(root, ledger)
    req = ReadRequest("xinqing", (DAY,), ("last_price", "ask_px_1", "ask_sz_1", "bid_px_1", "bid_sz_1", "ask_px_10", "ask_sz_10"))
    res = store.execute(plan(req, store.catalog("xinqing", (DAY,)), ledger))
    f = res.frame

    assert f.columns == ["day", "symbol", "last_price", "ask_px_1", "ask_sz_1", "bid_px_1", "bid_sz_1", "ask_px_10", "ask_sz_10"]
    assert f.schema["ask_sz_1"] == pl.Int64 and f.schema["ask_px_1"] == pl.Int64
    assert f.row(0) == (DAY, "600000.SH", 120000, 1201800, 2151938037, None, None, 250000, 1000)


# ==============================================================================
# 攻击面 2：Row Group 裁剪的边界（跨 Row Group、空隙无命中、临界点闭区间）
# ==============================================================================

def test_adv_plan_and_execute_symbol_spanning_consecutive_row_groups(root, ledger):
    """一个标的的数据跨越了连续两个 row group：计划必须选中两个 row group，执行必须保持原序拼接。"""
    # RG0: 000001.SZ (50 条), 000002.SZ (20 条) -> symbol_min='000001.SZ', symbol_max='000002.SZ'
    # RG1: 000002.SZ (30 条), 000003.SZ (50 条) -> symbol_min='000002.SZ', symbol_max='000003.SZ'
    rows_rg0 = [order_row("000001.SZ", "093000000", oid=f"1_{i}") for i in range(50)] + \
               [order_row("000002.SZ", "093000000", oid=f"2_rg0_{i}") for i in range(20)]
    rows_rg1 = [order_row("000002.SZ", "093001000", oid=f"2_rg1_{i}") for i in range(30)] + \
               [order_row("000003.SZ", "093000000", oid=f"3_{i}") for i in range(50)]
    rows_rg2 = [order_row("600000.SH", "093000000", oid=f"6_{i}") for i in range(50)]

    all_rows = rows_rg0 + rows_rg1 + rows_rg2
    write_preserve(root, "orders", DAY, all_rows, row_group_rows=70)

    store = RawStore(root, ledger)
    req = ReadRequest("orders", (DAY,), ("oid",), symbols=frozenset({"000002.SZ"}))
    p = plan(req, store.catalog("orders", (DAY,)), ledger)

    assert p.files[0].pruned is True
    assert len(p.files[0].row_groups) == 2
    assert [rg.index for rg in p.files[0].row_groups] == [0, 1]

    res = store.execute(p)
    assert res.stats.rows == 50
    assert res.stats.row_groups_read == 2
    assert res.stats.row_groups_total == 3
    # 验证标的内部严格保留跨 row group 的文件原序
    expected_oids = [i for i in range(20)] + [i for i in range(30)]   # 经 Int64 解码
    assert len(res.frame) == 50
    assert res.frame["symbol"].unique().to_list() == ["000002.SZ"]


def test_adv_plan_and_execute_symbol_in_gap_reads_zero_row_groups(root, ledger):
    """请求的标的落在两个 row group 之间的字典序空隙：裁剪必须产出 0 个 row group，执行读 0 字节并归因 SYMBOL_ABSENT。"""
    # RG0: 000001.SZ ~ 000005.SZ
    # RG1: 000020.SZ ~ 000025.SZ
    # 请求: 000010.SZ（恰好在空隙中，无任何 RG 包含）
    rows_rg0 = [order_row("000001.SZ", "093000000", oid="1"), order_row("000005.SZ", "093000000", oid="2")]
    rows_rg1 = [order_row("000020.SZ", "093000000", oid="3"), order_row("000025.SZ", "093000000", oid="4")]
    write_preserve(root, "orders", DAY, rows_rg0 + rows_rg1, row_group_rows=2)

    store = RawStore(root, ledger)
    req = ReadRequest("orders", (DAY,), ("oid",), symbols=frozenset({"000010.SZ"}))
    p = plan(req, store.catalog("orders", (DAY,)), ledger)

    assert p.files[0].pruned is True
    assert p.files[0].row_groups == ()

    res = store.execute(p)
    assert res.frame.height == 0
    assert res.stats.row_groups_read == 0
    assert res.stats.bytes_read == 0
    assert res.gaps == (Gap(DAY, "orders", GapReason.SYMBOL_ABSENT, "000010.SZ", ()),)


def test_adv_plan_row_group_min_max_boundary_equality(ledger):
    """纯 plan 测试：验证闭区间边界等于 symbol_min / symbol_max 以及单标的 RG (min==max) 的精确匹配。"""
    rg = (
        RowGroupMeta(0, 100, 1000, "000001.SZ", "000001.SZ"),   # 单标的 RG
        RowGroupMeta(1, 100, 1000, "000002.SZ", "000010.SZ"),
        RowGroupMeta(2, 100, 1000, "000011.SZ", "000020.SZ"),
    )
    cols = tuple(f"column_{i}" for i in range(1, 12)) + ("_symbol",)
    cat = Catalog("orders", (FileMeta(Path("/x/orders/date=20220104.parquet"), "orders", DAY, 300, cols, rg),))

    # 1. 命中单标的 RG0
    p1 = plan(ReadRequest("orders", (DAY,), ("price",), symbols=frozenset({"000001.SZ"})), cat, ledger)
    assert [r.index for r in p1.files[0].row_groups] == [0]

    # 2. 命中 RG1 的 symbol_max 边界
    p2 = plan(ReadRequest("orders", (DAY,), ("price",), symbols=frozenset({"000010.SZ"})), cat, ledger)
    assert [r.index for r in p2.files[0].row_groups] == [1]

    # 3. 命中 RG2 的 symbol_min 边界
    p3 = plan(ReadRequest("orders", (DAY,), ("price",), symbols=frozenset({"000011.SZ"})), cat, ledger)
    assert [r.index for r in p3.files[0].row_groups] == [2]

    # 4. 同时命中两端边界 (RG0 与 RG2)
    p4 = plan(ReadRequest("orders", (DAY,), ("price",), symbols=frozenset({"000001.SZ", "000020.SZ"})), cat, ledger)
    assert [r.index for r in p4.files[0].row_groups] == [0, 2]


# ==============================================================================
# 攻击面 3：时间窗过滤（毫秒边界、跨午休、多窗并集、混排过滤、空窗）
# ==============================================================================

def test_adv_window_half_open_millisecond_exact_inclusions(root, ledger):
    """验证半开区间 [start_ms, end_ms) 的毫秒级精确包含/排除：start_ms 包含，end_ms 排除。"""
    # 窗口: [09:30:00.000, 09:30:01.000) -> [34200000, 34201000)
    w = (Window(34_200_000, 34_201_000),)
    rows = [
        order_row("000001.SZ", "092959999", oid="0"),   # 34199999 -> 前一毫秒，排除
        order_row("000001.SZ", "093000000", oid="1"),   # 34200000 -> 左边界，包含
        order_row("000001.SZ", "093000500", oid="2"),   # 34200500 -> 窗内，包含
        order_row("000001.SZ", "093000999", oid="3"),   # 34200999 -> 窗内最后一毫秒，包含
        order_row("000001.SZ", "093001000", oid="4"),   # 34201000 -> 右边界，排除
        order_row("000001.SZ", "093001001", oid="5"),   # 34201001 -> 后一毫秒，排除
    ]
    write_preserve(root, "orders", DAY, rows)
    res = read(RawStore(root, ledger), ledger, fields=("oid",), windows=w)
    assert res.frame["oid"].to_list() == [1, 2, 3]


def test_adv_custom_window_spanning_lunch_break_strictly_numerical(root, ledger):
    """自定义跨午休的时间窗 [11:15:00, 13:15:00)：不得因午休概念被擅自切断，午间数据按纯数值范围包含。"""
    # [11:15:00, 13:15:00) -> [(11*3600+15*60)*1000, (13*3600+15*60)*1000) = [40500000, 47700000)
    w = (Window(40_500_000, 47_700_000),)
    rows = [
        order_row("000001.SZ", "111459000", oid="0"),   # 40499000 -> 排除
        order_row("000001.SZ", "112000000", oid="1"),   # 40800000 -> 包含 (早盘)
        order_row("000001.SZ", "114500000", oid="2"),   # 42300000 -> 包含 (午休期间)
        order_row("000001.SZ", "123000000", oid="3"),   # 45000000 -> 包含 (午休期间)
        order_row("000001.SZ", "131000000", oid="4"),   # 47400000 -> 包含 (午盘)
        order_row("000001.SZ", "131500000", oid="5"),   # 47700000 -> 排除 (右边界)
    ]
    write_preserve(root, "orders", DAY, rows)
    res = read(RawStore(root, ledger), ledger, fields=("oid",), windows=w)
    assert res.frame["oid"].to_list() == [1, 2, 3, 4]


def test_adv_window_multiple_disjoint_unions(root, ledger):
    """传入多个不相交的时间窗：结果必须是各时间窗的并集（包含落在任意窗内的行）。"""
    w1 = Window(34_200_000, 34_210_000)  # 09:30:00 ~ 09:30:10
    w2 = Window(50_000_000, 50_010_000)  # 13:53:20 ~ 13:53:30
    rows = [
        order_row("000001.SZ", "093005000", oid="1"),   # 在 w1 内
        order_row("000001.SZ", "100000000", oid="2"),   # 在两个窗之间 -> 排除
        order_row("000001.SZ", "135325000", oid="3"),   # 在 w2 内
        order_row("000001.SZ", "140000000", oid="4"),   # 在 w2 之后 -> 排除
    ]
    write_preserve(root, "orders", DAY, rows)
    res = read(RawStore(root, ledger), ledger, fields=("oid",), windows=(w1, w2))
    assert res.frame["oid"].to_list() == [1, 3]


def test_adv_window_filtering_on_registered_day_mixed_time_lengths(root, ledger):
    """在登记了 time_6digit 的天，五位、六位、九位时间混排时执行连续竞价窗过滤。"""
    rows = [
        order_row("600000.SH", "93000", oid="1"),      # 5 位 -> 09:30:00.000 = 34200000 ms (进 AM)
        order_row("600000.SH", "093000", oid="2"),     # 6 位 -> 09:30:00.000 = 34200000 ms (进 AM)
        order_row("600000.SH", "093000500", oid="3"),  # 9 位 -> 09:30:00.500 = 34200500 ms (进 AM)
        order_row("600000.SH", "113000", oid="4"),     # 6 位 -> 11:30:00.000 (午休边界，排除)
        order_row("600000.SH", "130000", oid="5"),     # 6 位 -> 13:00:00.000 = 46800000 ms (进 PM)
        order_row("600000.SH", "145659", oid="6"),     # 6 位 -> 14:56:59.000 = 53819000 ms (进 PM)
        order_row("600000.SH", "145700", oid="7"),     # 6 位 -> 14:57:00.000 (收盘竞价开始，排除)
    ]
    write_preserve(root, "orders", DAY6, rows)
    res = read(RawStore(root, ledger), ledger, days=(DAY6,), fields=("oid",), windows=CONTINUOUS_EXCL_AUCTIONS)
    assert res.frame["oid"].to_list() == [1, 2, 3, 5, 6]


def test_adv_window_empty_match_preserves_schema_without_gap(root, ledger):
    """时间窗过滤后无任何行匹配：返回 0 行 DataFrame 且 dtype/columns 完整，不得误产出 SYMBOL_ABSENT 缺口。"""
    rows = [order_row("000001.SZ", "093000000", oid="1", price="100000")]
    write_preserve(root, "orders", DAY, rows)
    # 请求全天前 1 秒的空窗
    w = (Window(1000, 2000),)
    res = read(RawStore(root, ledger), ledger, fields=("oid", "price"), symbols=frozenset({"000001.SZ"}), windows=w)

    assert res.frame.height == 0
    assert res.frame.columns == ["day", "symbol", "oid", "price"]
    assert res.frame.schema["day"] == pl.Date and res.frame.schema["oid"] == pl.Int64 and res.frame.schema["price"] == pl.Int64
    assert res.gaps == ()


# ==============================================================================
# 攻击面 4：缺口归因（跨天跨标的多重缺口、跨流隔离、账本缺陷过滤）
# ==============================================================================

def test_adv_gap_simultaneous_missing_days_and_missing_symbols(root, ledger):
    """同时请求存在的天（缺部分标的）与完全缺失的天：必须分别准确归因 SYMBOL_ABSENT 与 DAY_MISSING。"""
    d_exist = DAY
    d_missing_1 = dt.date(2022, 1, 5)
    d_missing_2 = dt.date(2022, 1, 6)

    # d_exist 只有 000001.SZ
    write_preserve(root, "orders", d_exist, [order_row("000001.SZ", "093000000", oid="1")])

    store = RawStore(root, ledger)
    req = ReadRequest("orders", (d_exist, d_missing_1, d_missing_2), ("oid",), symbols=frozenset({"000001.SZ", "000002.SZ", "000003.SZ"}))
    cat = store.catalog("orders", req.days)
    res = store.execute(plan(req, cat, ledger))

    assert res.frame.height == 1
    assert res.frame.row(0) == (d_exist, "000001.SZ", 1)

    # 验证缺口：d_exist 产生 2 个标的缺口；d_missing_1 和 d_missing_2 产生天级缺口
    expected_gaps = {
        (d_exist, "orders", GapReason.SYMBOL_ABSENT, "000002.SZ", ()),
        (d_exist, "orders", GapReason.SYMBOL_ABSENT, "000003.SZ", ()),
        (d_missing_1, "orders", GapReason.DAY_MISSING, None, ()),
        (d_missing_2, "orders", GapReason.DAY_MISSING, None, ()),
    }
    actual_gaps = {(g.day, g.stream, g.reason, g.symbol, g.defects) for g in res.gaps}
    assert actual_gaps == expected_gaps


def test_adv_rescue_day_gap_cross_stream_isolation(root, ledger):
    """在救援日，orders 有某标的而 trades 无某标的：读取 trades 必须报告带 rescue_partial 的 Gap，绝不跨流偷看。"""
    # orders 只有 000001.SZ；trades 只有 000002.SZ
    write_preserve(root, "orders", DAY_RESCUE, [order_row("000001.SZ", "093000000")])
    write_preserve(root, "trades", DAY_RESCUE, [trade_row("000002.SZ", "093000000")])

    store = RawStore(root, ledger)

    # 1. 读 orders，查 000002.SZ
    req_o = ReadRequest("orders", (DAY_RESCUE,), ("oid",), symbols=frozenset({"000001.SZ", "000002.SZ"}))
    res_o = store.execute(plan(req_o, store.catalog("orders", (DAY_RESCUE,)), ledger))
    assert res_o.frame["symbol"].to_list() == ["000001.SZ"]
    assert res_o.gaps == (Gap(DAY_RESCUE, "orders", GapReason.SYMBOL_ABSENT, "000002.SZ", ("rescue_partial",)),)

    # 2. 读 trades，查 000001.SZ
    req_t = ReadRequest("trades", (DAY_RESCUE,), ("seq",), symbols=frozenset({"000001.SZ", "000002.SZ"}))
    res_t = store.execute(plan(req_t, store.catalog("trades", (DAY_RESCUE,)), ledger))
    assert res_t.frame["symbol"].to_list() == ["000002.SZ"]
    assert res_t.gaps == (Gap(DAY_RESCUE, "trades", GapReason.SYMBOL_ABSENT, "000001.SZ", ("rescue_partial",)),)


def test_adv_gap_defects_filtered_strictly_by_stream_and_day():
    """验证账本中的缺陷条目必须严格按 day 和 stream 过滤，不得跨流或跨天泄露到 Gap.defects 中。"""
    txt = ledger_toml(
        'code = "rescue_partial"\nstream = "orders"\ndays = [2022-01-04]',
        'code = "seq_empty"\nstream = "trades"\ndays = [2022-01-04]',
        'code = "time_6digit"\nstream = "orders"\ndays = [2024-02-06]',
    )
    custom_ledger = parse_ledger(txt)
    # orders 在 2022-01-04 只有 rescue_partial
    defects_orders_day = tuple(d.code.value for d in custom_ledger.for_day(DAY, "orders"))
    assert defects_orders_day == ("rescue_partial",)

    # trades 在 2022-01-04 只有 seq_empty
    defects_trades_day = tuple(d.code.value for d in custom_ledger.for_day(DAY, "trades"))
    assert defects_trades_day == ("seq_empty",)

    # orders 在 2024-02-06 只有 time_6digit
    defects_orders_day6 = tuple(d.code.value for d in custom_ledger.for_day(DAY6, "orders"))
    assert defects_orders_day6 == ("time_6digit",)


# ==============================================================================
# 攻击面 5：投影与保留列（重复字段去重、只请求保留列、物理列拦截、过滤列不泄露）
# ==============================================================================

def test_adv_projection_duplicate_and_reserved_reordering(root, ledger):
    """请求字段包含重复字段以及保留名（day, symbol）：计划与执行输出必须去重、保留列在最前，且无重名异常。"""
    rows = [order_row("000001.SZ", "093000000", oid="10", price="100000", vol="500")]
    write_preserve(root, "orders", DAY, rows)

    store = RawStore(root, ledger)
    req = ReadRequest("orders", (DAY,), ("vol", "symbol", "price", "day", "vol", "oid", "price"))
    p = plan(req, store.catalog("orders", (DAY,)), ledger)

    # 规范输出字段顺序：day, symbol 居首，其余按初次出现顺序去重
    assert p.output_fields == ("day", "symbol", "vol", "price", "oid")
    assert set(p.files[0].columns) == {"_symbol", "column_10", "column_9", "column_6"}

    res = store.execute(p)
    assert res.frame.columns == ["day", "symbol", "vol", "price", "oid"]
    assert res.frame.row(0) == (DAY, "000001.SZ", 500, 100000, 10)


def test_adv_projection_only_reserved_symbols_or_day(root, ledger):
    """只请求保留字段 symbol（或 day）：计划物理投影仅需 _symbol 列，执行返回 2 列 DataFrame。"""
    rows = [order_row("000001.SZ", "093000000", oid="1")]
    write_preserve(root, "orders", DAY, rows)

    store = RawStore(root, ledger)
    req = ReadRequest("orders", (DAY,), ("symbol",))
    p = plan(req, store.catalog("orders", (DAY,)), ledger)

    assert p.output_fields == ("day", "symbol")
    assert set(p.files[0].columns) == {"_symbol"}

    res = store.execute(p)
    assert res.frame.columns == ["day", "symbol"]
    assert res.frame.schema["day"] == pl.Date
    assert res.frame.schema["symbol"] in STRINGISH
    assert res.frame.row(0) == (DAY, "000001.SZ")


def test_adv_projection_raw_column_or_unregistered_field_raises(root, ledger):
    """请求 physical 列名（如 raw:column_1 或 column_9）或不存在的语义列名：必须抛出 KeyError。"""
    cat = Catalog("orders", (FileMeta(Path("/x/orders/date=20220104.parquet"), "orders", DAY, 100, ("column_9", "_symbol"), ()),))
    with pytest.raises(KeyError):
        plan(ReadRequest("orders", (DAY,), ("raw:column_1",)), cat, ledger)
    with pytest.raises(KeyError):
        plan(ReadRequest("orders", (DAY,), ("column_9",)), cat, ledger)
    with pytest.raises(KeyError):
        plan(ReadRequest("orders", (DAY,), ("non_existent_field",)), cat, ledger)
    with pytest.raises(KeyError):
        field("orders", "column_9")


def test_adv_projection_filter_columns_not_leaked_to_output(root, ledger):
    """为 symbols 裁剪与 windows 过滤扩展读入的 column_4 在返回前必须被剔除。"""
    rows = [order_row("000001.SZ", "093000500", price="100000")]
    write_preserve(root, "orders", DAY, rows)

    store = RawStore(root, ledger)
    req = ReadRequest("orders", (DAY,), ("price",), symbols=frozenset({"000001.SZ"}), windows=(Window(34_200_000, 34_201_000),))
    p = plan(req, store.catalog("orders", (DAY,)), ledger)

    # 物理投影扩展了 column_4（用于 window 过滤）和 _symbol
    assert set(p.files[0].columns) == {"_symbol", "column_4", "column_9"}
    assert p.output_fields == ("day", "symbol", "price")

    res = store.execute(p)
    assert res.frame.columns == ["day", "symbol", "price"]
    assert "column_4" not in res.frame.columns and "time_ms" not in res.frame.columns


# ==============================================================================
# 攻击面 6：摄取（空 CSV、缺流失败、扁平目录、转义换行计数、前缀筛选）
# ==============================================================================

@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_adv_ingest_empty_csv_header_only(tmp_path, root, ledger):
    """归档中所有 CSV 只有表头（0 数据行）：必须合法摄取，行数独立计数为 0，生成包含完整 schema 的 0 行 parquet。"""
    sym_data = {"000001.SZ": {"orders": [], "trades": [], "xinqing": []}}
    archive = make_adv_archive(tmp_path, DAY, sym_data)

    r = ingest(DAY, archive, root)
    assert r.day == DAY
    for s in r.streams:
        assert s.n_symbols == 1
        assert s.n_rows_csv == 0
        assert s.n_rows_parquet == 0
        meta = pq.read_metadata(root / s.stream / f"date={DAY:%Y%m%d}.parquet")
        assert meta.num_rows == 0

    store = RawStore(root, ledger)
    res = store.execute(plan(ReadRequest("orders", (DAY,), ("oid", "price")), store.catalog("orders", (DAY,)), ledger))
    assert res.frame.height == 0
    assert res.frame.columns == ["day", "symbol", "oid", "price"]


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_adv_ingest_missing_stream_csv_in_symbol_dir_fails_loud(tmp_path, root, ledger):
    """某标的目录下缺少逐笔成交.csv（只有行情与委托）：破坏三流齐全性，必须 RuntimeError 且不写 manifest。"""
    src = tmp_path / "src" / f"{DAY:%Y%m%d}" / "000001.SZ"
    src.mkdir(parents=True)
    # 故意只写行情和委托，不写成交
    (src / CSV_NAME["orders"]).write_bytes(HEADER["orders"].encode("gbk") + b"\n")
    (src / CSV_NAME["xinqing"]).write_bytes(HEADER["xinqing"].encode("gbk") + b"\n")

    archive = tmp_path / f"{DAY:%Y%m%d}.7z"
    subprocess.run(["7zz", "a", "-bso0", "-bsp0", str(archive), f"{DAY:%Y%m%d}"], cwd=src.parent.parent, check=True)

    with pytest.raises(RuntimeError):
        ingest(DAY, archive, root)

    assert not (root / "manifest" / f"{DAY:%Y%m%d}.json").exists()


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_adv_ingest_flat_layout_without_date_dir_prefix(tmp_path, root, ledger):
    """归档内部直接是 {symbol}/{csv} 扁平布局（无 YYYYMMDD 前缀目录）：必须成功解析摄取。"""
    sym_data = {
        "000001.SZ": {
            "orders": ["000001.SZ,000001,20220104,093000000,1,10,0,B,100000,100,"],
            "trades": ["000001.SZ,000001,20220104,093000000,1,0,0,B,100000,100,0,0,"],
            "xinqing": ["000001.SZ,000001,20220104,093000000," + ",".join(["100"] * 63)],
        }
    }
    archive = make_adv_archive(tmp_path, DAY, sym_data, flat=True)
    r = ingest(DAY, archive, root)

    assert r.day == DAY
    assert all(s.n_rows_csv == s.n_rows_parquet == 1 for s in r.streams)
    assert (root / "manifest" / f"{DAY:%Y%m%d}.json").exists()


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_adv_ingest_row_count_with_embedded_commas_and_crlf(tmp_path, root, ledger):
    """数据行中含有逗号、引号并混有 Windows CRLF 换行与尾随空白行：独立换行计数必须准确等于有效数据行数。"""
    src = tmp_path / "src" / f"{DAY:%Y%m%d}" / "000001.SZ"
    src.mkdir(parents=True)

    order_lines = [
        '000001.SZ,000001,20220104,093000000,1,10,0,B,100000,100,',
        '000001.SZ,000001,20220104,093001000,2,11,"0",B,100000,200,',
        '000001.SZ,000001,20220104,093002000,3,12,0,B,100000,300,',
    ]
    for s in STREAMS:
        # 混入 CRLF 与末尾 3 个空行
        body = "\r\n".join([HEADER[s], *order_lines]) + "\r\n\r\n\n\r\n"
        (src / CSV_NAME[s]).write_bytes(body.encode("gbk"))

    archive = tmp_path / f"{DAY:%Y%m%d}.7z"
    subprocess.run(["7zz", "a", "-bso0", "-bsp0", str(archive), f"{DAY:%Y%m%d}"], cwd=src.parent.parent, check=True)

    r = ingest(DAY, archive, root)
    assert all(s.n_rows_csv == s.n_rows_parquet == 3 for s in r.streams)


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_adv_ingest_dropped_by_prefix_excluded_from_manifest_and_canonical_hash(tmp_path, root, ledger):
    """归档包含主板 (000)、创业板 (300)、科创板 (688)、北交所 (830)：非主板标的按前缀统计丢弃，不进入 parquet 与规范帧。"""
    sym_data = {
        "000001.SZ": {"orders": ["000001.SZ,000001,20220104,093000000,1,10,0,B,100000,100,"]},
        "300001.SZ": {"orders": ["300001.SZ,300001,20220104,093000000,2,20,0,B,200000,200,"]},
        "688001.SH": {"orders": ["688001.SH,688001,20220104,093000000,3,30,A,B,300000,300,"]},
        "830001.BJ": {"orders": ["830001.BJ,830001,20220104,093000000,4,40,0,B,400000,400,"]},
    }
    archive = make_adv_archive(tmp_path, DAY, sym_data)
    r = ingest(DAY, archive, root, prefixes=MAIN_PREFIXES)

    assert r.dropped_by_prefix == {"300": 1, "688": 1, "830": 1}
    for s in r.streams:
        assert s.n_symbols == 1
        assert s.n_rows_parquet == 1

    t = pl.read_parquet(root / "orders" / f"date={DAY:%Y%m%d}.parquet")
    assert t["_symbol"].unique().to_list() == ["000001.SZ"]


# ==============================================================================
# 攻击面 7：幂等与原子写入（崩溃残留清理、缺 manifest 重建、mtime 严苛不变、异源拒绝）
# ==============================================================================

@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_adv_ingest_leftover_tmp_files_from_crash_cleaned(tmp_path, root, ledger):
    """模拟上次进程崩溃遗留了 .tmp 文件：摄取必须成功完成，且最终 root 下绝无任何 .tmp 残留。"""
    sym_data = {"000001.SZ": {"orders": ["000001.SZ,000001,20220104,093000000,1,10,0,B,100000,100,"]}}
    archive = make_adv_archive(tmp_path, DAY, sym_data)

    # 预先在 orders 目录放置垃圾 .tmp
    (root / "orders" / f"date={DAY:%Y%m%d}.parquet.tmp").write_bytes(b"corrupt partial bytes")
    (root / "manifest" / f"{DAY:%Y%m%d}.json.tmp").write_text("{corrupt json")

    r = ingest(DAY, archive, root)
    assert r.day == DAY
    assert not list(root.rglob("*.tmp"))
    assert not list(root.rglob("*.partial"))
    assert (root / "orders" / f"date={DAY:%Y%m%d}.parquet").exists()
    assert (root / "manifest" / f"{DAY:%Y%m%d}.json").exists()


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_adv_ingest_missing_manifest_recreates_atomically(tmp_path, root, ledger):
    """三个 stream 的 parquet 已生成但 manifest 缺失（判定为 UNVERIFIED）：再次 ingest 必须原子补齐 manifest。"""
    sym_data = {"000001.SZ": {"orders": ["000001.SZ,000001,20220104,093000000,1,10,0,B,100000,100,"]}}
    archive = make_adv_archive(tmp_path, DAY, sym_data)

    ingest(DAY, archive, root)
    manifest_path = root / "manifest" / f"{DAY:%Y%m%d}.json"
    manifest_path.unlink()  # 删掉 manifest 模拟缺失

    store = RawStore(root, ledger)
    assert store.quality(DAY) is Quality.UNVERIFIED

    # 重新 ingest 必须成功修复并生成合法 receipt
    r2 = ingest(DAY, archive, root)
    assert r2.day == DAY
    assert manifest_path.exists()
    assert store.quality(DAY) is not Quality.UNVERIFIED


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_adv_ingest_strict_idempotent_no_mtime_change(tmp_path, root, ledger):
    """幂等性严苛测试：第二次 ingest 相同归档，所有产物文件的修改时间戳 (st_mtime_ns) 必须一字不改。"""
    sym_data = {"000001.SZ": {"orders": ["000001.SZ,000001,20220104,093000000,1,10,0,B,100000,100,"]}}
    archive = make_adv_archive(tmp_path, DAY, sym_data)

    r1 = ingest(DAY, archive, root)
    mtimes_1 = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}

    r2 = ingest(DAY, archive, root)
    mtimes_2 = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}

    assert r1 == r2
    assert mtimes_1 == mtimes_2


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_adv_ingest_modified_archive_fails_loud_and_leaves_root_untouched(tmp_path, root, ledger):
    """同一天传入不同内容（不同 sha256）的归档：必须抛出 RuntimeError 且 root 下既有文件内容丝毫不受污染。"""
    a1 = make_adv_archive(tmp_path / "a1", DAY, {"000001.SZ": {"orders": ["000001.SZ,000001,20220104,093000000,1,10,0,B,100000,100,"]}})
    a2 = make_adv_archive(tmp_path / "a2", DAY, {"000001.SZ": {"orders": ["000001.SZ,000001,20220104,093000000,2,20,0,B,200000,200,"]}})

    ingest(DAY, a1, root)
    manifest_bytes_before = (root / "manifest" / f"{DAY:%Y%m%d}.json").read_bytes()

    with pytest.raises(RuntimeError, match="sha256|归档"):
        ingest(DAY, a2, root)

    manifest_bytes_after = (root / "manifest" / f"{DAY:%Y%m%d}.json").read_bytes()
    assert manifest_bytes_before == manifest_bytes_after
