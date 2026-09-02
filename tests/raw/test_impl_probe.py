"""实现级探针测试：攻 raw 实现里契约没覆盖到的边界、性质、bug。

约定：
- 不重复 tests/raw/test_{store,plan,ingest,adversarial}.py 已覆盖的契约；
- 期望值来自独立事实（polars/pyarrow 自身行为、Python int() 规则、CSV 字节流、footer 元数据），
  不复制实现算法；
- 每条测试 docstring 标注"独立事实"与"攻的点"。

针对实现分支：decode.to_int64 / decode.to_time_ms / _column_count(头-only) /
_write_parquet(空 frame) / .tmp 残留命名约定 / 读取路径是否走 pyarrow pre_buffer=True /
polars.read_parquet 是否被调用 / inspect_raw 是否解码 / plan 时已知字段约束 /
ledger 解析严格性 / 空窗 / 空 result DataFrame schema / 重复 days。
"""

from __future__ import annotations

import datetime as dt
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ftbv2.core.raw.decode import in_windows, to_int64, to_time_ms
from ftbv2.core.raw.ledger import parse_ledger
from ftbv2.core.raw.plan import plan
from ftbv2.core.raw.schema import CSV_NAME, MAIN_PREFIXES, STREAMS, field
from ftbv2.core.raw.types import (
    Catalog, FileMeta, Gap, GapReason, ReadRequest, RowGroupMeta, ScanPlan, Window,
)
from ftbv2.io.raw.ingest import ingest
from ftbv2.io.raw.store import RawStore
from tests.raw.conftest import (
    DAY, DAY6, DAY_RESCUE, NCOLS, NUL, RESCUE, TIME6,
    ledger_toml, order_row, trade_row, write_preserve,
)

HEADER = {
    "orders": "万得代码,交易所代码,自然日,时间,委托编号,交易所委托号,委托类型,委托代码,委托价格,委托数量,",
    "trades": "万得代码,交易所代码,自然日,时间,成交编号,成交代码,委托代码,BS标志,成交价格,成交数量,叫卖序号,叫买序号,",
    "xinqing": "万得代码,交易所代码,自然日,时间," + ",".join(f"f{i}" for i in range(5, 68)),
}


def _build_archive(tmp_path: Path, day: dt.date, symbols_data: dict[str, dict[str, list[str]]], flat: bool = False) -> Path:
    src = tmp_path / "src"
    base = src if flat else (src / f"{day:%Y%m%d}")
    for sym, stream_dict in symbols_data.items():
        d = base / sym
        d.mkdir(parents=True, exist_ok=True)
        for s in STREAMS:
            lines = stream_dict.get(s, [])
            body = "\n".join([HEADER[s], *lines]) + "\n"
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


def _exec_orders(store: RawStore, ledger, **kw) -> "tuple":
    req = ReadRequest(**{"stream": "orders", "days": (DAY,), "fields": ("oid", "side", "type", "price", "vol", "time_ms"), **kw})
    return store.execute(plan(req, store.catalog("orders", (DAY,)), ledger))


# ==============================================================================
# 1) decode.to_int64 边界
# ==============================================================================


def test_impl_to_int64_pure_null_inputs_distinct():
    """独立事实：Python int() 对非整数字面量抛 ValueError；polars str→Float64→Int64
路径对合法浮点字符串执行截断整数转换（'1.0' → 1.0 → 1）。
    攻：to_int64 对每个分支输入的空/空字符/纯空白/NUL/字母/带符号 0 的判定。"""
    cases = [
        ("", None),
        (" ", None),
        ("\x00", None),
        ("\x00\x00", None),
        ("abc", None),
        ("1.0", 1),
        ("-1.5e+02", -150),
        ("-1", -1),
        ("+1", 1),
        ("0", 0),
        ("-0", 0),
        ("00", 0),
        ("+0", 0),
        ("18446744073709551615", None),
    ]
    df = pl.DataFrame({"x": [v for v, _ in cases]}).select(to_int64("x").alias("x"))
    assert df["x"].to_list() == [exp for _, exp in cases]


def test_impl_to_int64_2_53_boundary():
    """独立事实：Float64 最大精确整数 = 2^53 = 9007199254740992；2^53+1 落到 2^53。
    攻：精确边界值保留；越界值经 Float64 中转丢精度 ⇒ null（不静默给错值）。"""
    # 巩固时修正：2^53+2 是偶数，float64 精确可表示，判定函数 int(float(s)) == int(s) 成立 ⇒ 保留
    ints = ["9007199254740992", "9007199254740993", "9007199254740994", "-9007199254740993"]
    out = pl.DataFrame({"x": ints}).select(to_int64("x").alias("x"))
    assert out["x"].to_list() == [9007199254740992, None, 9007199254740994, None]


def test_impl_to_int64_on_empty_and_all_null_frames():
    """独立事实：polars 表达式对 0 行帧、纯 null 列、单 null 行均应干净执行。
    攻：to_int64 在退化输入上的健壮性（不得抛、不改变 dtype）。"""
    for df in (
        pl.DataFrame({"x": []}, schema={"x": pl.String}),
        pl.DataFrame({"x": [None]}, schema={"x": pl.String}),
        pl.DataFrame({"x": [None, None, None]}, schema={"x": pl.String}),
    ):
        out = df.select(to_int64("x").alias("x"))
        assert out.schema["x"] == pl.Int64
        assert out.height == df.height


# ==============================================================================
# 2) decode.to_time_ms 边界
# ==============================================================================


def test_impl_to_time_ms_length_matrix():
    """独立事实：按字符串长度分桶；长度 ∈ {5,6} 仅在 allow_6digit=True 时解析；
    长度 ∈ {0..4,7,8,10,11,12} 一律 null；数字越界（h>23, m>59, s>59）落 null。
    攻：每个长度的 if/else 分支 + 越界判定。"""
    def build(length: int) -> str:
        return {
            0: "", 1: "0", 2: "00", 3: "000", 4: "0000",
            5: "93000", 6: "093000", 7: "0930000", 8: "09300000",
            9: "093000000", 10: "0930000000", 11: "09300000000", 12: "093000000000",
        }[length]

    inputs = [build(L) for L in range(0, 13)]
    df = pl.DataFrame({"t": inputs})

    base_ms = (9 * 3600 + 30 * 60) * 1000
    out_true = df.select(to_time_ms("t", allow_6digit=True).alias("t"))
    expected_true = [None] * 5 + [base_ms, base_ms, None, None, base_ms, None, None, None]
    assert out_true["t"].to_list() == expected_true

    out_false = df.select(to_time_ms("t", allow_6digit=False).alias("t"))
    expected_false = [None] * 9 + [base_ms] + [None] * 3
    assert out_false["t"].to_list() == expected_false


def test_impl_to_time_ms_strip_chars_handles_tabs_newlines():
    """独立事实：str.strip_chars 默认去掉首尾空白字符（含 tab/CR/LF）。
    攻：to_time_ms 的 strip_chars 路径。"""
    df = pl.DataFrame({
        "t": [" \t093000500\r", "\n\n093000000\n\n", " ", "", "093000500"],
    })
    out = df.select(to_time_ms("t", allow_6digit=True).alias("t"))
    base_ms = (9 * 3600 + 30 * 60) * 1000
    expected = [base_ms + 500, base_ms, None, None, base_ms + 500]
    assert out["t"].to_list() == expected


def test_impl_to_time_ms_6digit_digit_overflow_returns_null():
    """独立事实：h>23 或 m>59 或 s>59 落 null（独立计算 h*3600+m*60+s 与有效区间对比）。
    攻：parse_ms 的 is_between 区间在六位输入上的执行。"""
    df = pl.DataFrame({"t": ["240000", "096000", "093060", "235959"]})
    out = df.select(to_time_ms("t", allow_6digit=True).alias("t"))
    assert out["t"].to_list() == [None, None, None, (23 * 3600 + 59 * 60 + 59) * 1000]


def test_impl_to_time_ms_allow_6digit_false_six_digit_no_raise():
    """独立事实：allow_6digit=False 时六位值归 null，不抛错（execute 的兜底层）。
    攻：长度分桶的 else 分支与无副作用保证。"""
    df = pl.DataFrame({"t": ["093000", "093001", ""]})
    out = df.select(to_time_ms("t", allow_6digit=False).alias("t"))
    assert out["t"].to_list() == [None, None, None]


def test_impl_in_windows_empty_tuple_is_false():
    """独立事实：in_windows() 的 for循环空元组等价于常量 False；不报错、不短路错。
    攻：in_windows 在 windows=() 时（虽 ReadRequest 已禁）的表达式求值。"""
    df = pl.DataFrame({"t": [0, 1000, 86_399_999]})
    out = df.select(in_windows("t", ()).alias("in"))
    assert out["in"].to_list() == [False, False, False]


# ==============================================================================
# 3) _store_impl.empty_output / 投影 / 字段
# ==============================================================================


def test_impl_empty_output_schema_matches_output_fields_kind(root, ledger):
    """独立事实：schema.Kind 表 ⇒ symbol/str/enum → String；time/int/price → Int64；day → Date。
    攻：_empty_output 在所有文件均无数据时产出 frame 的 dtype。"""
    write_preserve(root, "orders", DAY, [])
    write_preserve(root, "trades", DAY, [])
    write_preserve(root, "xinqing", DAY, [])
    store = RawStore(root, ledger)
    req = ReadRequest("orders", (DAY,), ("oid", "side", "price", "vol", "time_ms"))
    res = store.execute(plan(req, store.catalog("orders", (DAY,)), ledger))
    assert res.frame.height == 0
    assert res.frame.columns == ["day", "symbol", "oid", "side", "price", "vol", "time_ms"]
    assert res.frame.schema["day"] == pl.Date
    assert res.frame.schema["symbol"] in (pl.String, pl.Categorical)
    assert res.frame.schema["oid"] == pl.Int64
    assert res.frame.schema["side"] in (pl.String, pl.Categorical)
    assert res.frame.schema["price"] == pl.Int64
    assert res.frame.schema["vol"] == pl.Int64
    assert res.frame.schema["time_ms"] == pl.Int64


def test_impl_plan_unknown_field_raises_keyerror():
    """独立事实：schema.FIELDS 是字段名唯一源；未登记名必须抛 KeyError。
    攻：plan() 内的 schema.field 调用与 schema.FIELDS 的键集。"""
    rg = (RowGroupMeta(0, 100, 1000, "000001.SZ", "000001.SZ"),)
    cols = tuple(f"column_{i}" for i in range(1, 12)) + ("_symbol",)
    cat = Catalog("orders", (FileMeta(Path("/x"), "orders", DAY, 100, cols, rg),))
    led = parse_ledger(ledger_toml())
    with pytest.raises(KeyError):
        plan(ReadRequest("orders", (DAY,), ("feature_x",)), cat, led)
    with pytest.raises(KeyError):
        plan(ReadRequest("orders", (DAY,), ("column_9",)), cat, led)
    with pytest.raises(KeyError):
        field("orders", "column_9")
    with pytest.raises(KeyError):
        field("not_a_stream", "x")


# ==============================================================================
# 4) ledger.parse_ledger 严格性
# ==============================================================================


def test_impl_ledger_parse_rejects_duplicates_and_unknown_codes():
    """独立事实：账本 schema 的必填/唯一性约束：id 重复、code 不在 DefectCode 枚举、
    stream 不在 STREAMS、缺 id 字段都必须抛 ValueError/KeyError，不静默。
    攻：parse_ledger 的循环与 DefectCode 转换。"""
    # ledger_toml 会自动编 id，这里要造重复 id 与非法值，直接写 TOML（巩固时修正：原写法在同一表里重复 id 键，是 TOML 语法错）
    dup = '[[defect]]\nid = "D001"\ncode = "time_6digit"\ndays = [2024-02-06]\n\n[[defect]]\nid = "D001"\ncode = "time_6digit"\ndays = [2024-02-07]\n'
    with pytest.raises(ValueError, match="重复"):
        parse_ledger(dup)
    with pytest.raises((ValueError, KeyError)):
        parse_ledger('[[defect]]\nid = "D001"\ncode = "no_such_code"\ndays = [2024-02-06]\n')
    with pytest.raises(ValueError, match="未知|stream|unknown"):
        parse_ledger('[[defect]]\nid = "D001"\ncode = "time_6digit"\nstream = "no_stream"\ndays = [2024-02-06]\n')
    with pytest.raises(KeyError):
        parse_ledger('[[defect]]\ncode = "time_6digit"\ndays = [2024-02-06]\n')


# ==============================================================================
# 5) 性质测试：随机夹具
# ==============================================================================


def _random_orders(seed: int, n: int) -> list[dict[str, str]]:
    rng = random.Random(seed)
    prefixes = ["000001.SZ", "000002.SZ", "000003.SZ", "600000.SH", "600001.SH", "603000.SH"]
    rows = []
    for i in range(n):
        sym = rng.choice(prefixes)
        hh = rng.choice([9, 10, 11, 13, 14])
        mm = rng.randint(0, 55)
        ss = rng.randint(0, 59)
        side = rng.choice(["B", "S", " "])
        rows.append(order_row(
            sym, f"{hh:02d}{mm:02d}{ss:02d}000",
            oid=str(i), typ="0", side=side,
            price=str(rng.randint(10000, 1000000)),
            vol=str(rng.randint(1, 1000)),
        ))
    return rows


def test_impl_property_filtered_output_is_subset_of_full_output(root, ledger):
    """独立事实：过滤只剔除、不增行；同一数据集的过滤输出 ⊆ 全量输出（按 (day,symbol,oid)）。
    攻：execute 的 symbol_exact / window post-filter 不引入幽灵行。"""
    rows = _random_orders(seed=1, n=30)
    write_preserve(root, "orders", DAY, rows)
    store = RawStore(root, ledger)
    cat = store.catalog("orders", (DAY,))

    full = store.execute(plan(ReadRequest("orders", (DAY,), ("oid", "price")), cat, ledger)).frame
    subset = store.execute(plan(
        ReadRequest("orders", (DAY,), ("oid", "price"),
 symbols=frozenset({"000001.SZ", "600000.SH"})),
        cat, ledger,
 )).frame

    def keyset(df):
        return set(zip(df["day"].to_list(),
 df["symbol"].cast(pl.String).to_list(),
                       df["oid"].to_list()))
    assert keyset(subset) <= keyset(full)


def test_impl_property_symbols_all_set_prunes_to_all_row_groups(root, ledger):
    """独立事实：symbols = 文件里所有标的的集合 ⇒ 每个 RG 的 [min,max] 至少与一个标的相交，
    statistics 裁剪全部命中；pruned=True 但 result.frame 与无裁剪全量结果同集。
    攻：_overlaps 的全覆盖语义与 pruner 不丢行。"""
    syms = ["000001.SZ", "000002.SZ", "600000.SH"]
    rows = [order_row(s, "093000000", oid=str(k)) for s in syms for k in range(10)]
    write_preserve(root, "orders", DAY, rows, row_group_rows=10)

    store = RawStore(root, ledger)
    cat = store.catalog("orders", (DAY,))

    req = ReadRequest("orders", (DAY,), ("oid",), symbols=frozenset(syms))
    p = plan(req, cat, ledger)
    assert p.files[0].pruned is True
    assert len(p.files[0].row_groups) == 3

    res = store.execute(p)
    full = store.execute(plan(ReadRequest("orders", (DAY,), ("oid",)), cat, ledger)).frame
    assert res.frame.sort("oid")["oid"].to_list() == full.sort("oid")["oid"].to_list()


def test_impl_property_multi_day_concat_equals_per_day_union(root, ledger):
    """独立事实：多日拼接按天级独立执行后 concat（行序 = 天序 × 文件序）；
    与各日单独执行结果之 union 在 (day,symbol,oid) 上严格相等。
    攻：execute 的多日路径与 _file_plans 不丢行不增行。"""
    rows1 = _random_orders(seed=11, n=20)
    rows2 = _random_orders(seed=22, n=20)
    day2 = dt.date(2024, 5, 7)
    write_preserve(root, "orders", DAY, rows1)
    write_preserve(root, "orders", day2, rows2)

    store = RawStore(root, ledger)

    def one(d):
        return store.execute(plan(
            ReadRequest("orders", (d,), ("oid",)),
            store.catalog("orders", (d,)), ledger,
        )).frame

    union = pl.concat([one(DAY), one(day2)], how="vertical")
    concat = store.execute(plan(
        ReadRequest("orders", (DAY, day2), ("oid",)),
        store.catalog("orders", (DAY, day2)), ledger,
    )).frame

    def keyset(df):
        return set(zip(df["day"].to_list(),
                       df["symbol"].cast(pl.String).to_list(),
                       df["oid"].to_list()))
    assert keyset(concat) == keyset(union)


def test_impl_property_int64_roundtrip_for_random_integer_strings():
    """独立事实：合法整数（远离 2^53 边界）经 str→Float64→Int64 路径恒等。
    攻：to_int64 在大样本上的数值保真（不退化、不静默截断）。"""
    rng = random.Random(42)
    ints = [rng.randint(-(2 ** 53) + 100, 2 ** 53 - 100) for _ in range(50)]
    df = pl.DataFrame({"x": [str(v) for v in ints]}).select(to_int64("x").alias("x"))
    assert df["x"].to_list() == ints


def test_impl_property_enum_unknown_values_are_identity(root, ledger):
    """独立事实：枚举列只过 column → alias，不解释；未知值（' ', '\x00', 'C', 'I' 等）原样保留。
    攻：_decode_field 对 enum kind 的 passthrough。"""
    weird = ["B", "S", " ", "", "C", "I", "J", "O", "\x00", "U", "X", "0", "1", "A", "D"]
    rows = [order_row("000001.SZ", "093000000", oid=str(i), side=v, typ=v) for i, v in enumerate(weird)]
    write_preserve(root, "orders", DAY, rows)
    res = _exec_orders(RawStore(root, ledger), ledger)
    df = res.frame.sort("oid")
    assert df["side"].cast(pl.String).to_list() == weird
    assert df["type"].cast(pl.String).to_list() == weird


# ==============================================================================
# 6) 摄取往返与 schema 一致性（潜在 bug 探针）
# ==============================================================================


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_impl_ingest_byte_equal_roundtrip_on_tricky_values(tmp_path, root, ledger):
    """独立事实：CSV 字节流经 ingest → preserve 后 inspect_raw 读回所有列（col_N + _symbol）
    必须与原始字段严格相等（包含空串、空格、NUL、UINT64 哨兵字符串、科学计数法）。
    攻：ingest → write_parquet → inspect_raw 的字符串保真全链路。"""
    src = tmp_path / "src" / f"{DAY:%Y%m%d}" / "000001.SZ"
    src.mkdir(parents=True)
    order_line = "000001.SZ,000001,20220104,093000000,1,18446744073709551615,0,B,1.2e5,\x00,"
    trade_line = "000001.SZ,000001,20220104,093000000,1,0,\x00,B,100000,100,0,0,"
    xinqing_line = "000001.SZ,000001,20220104,093000000," + ",".join(["100"] * 63)
    (src / CSV_NAME["orders"]).write_bytes((HEADER["orders"] + "\n" + order_line + "\n").encode("gbk"))
    (src / CSV_NAME["trades"]).write_bytes((HEADER["trades"] + "\n" + trade_line + "\n").encode("gbk"))
    (src / CSV_NAME["xinqing"]).write_bytes((HEADER["xinqing"] + "\n" + xinqing_line + "\n").encode("gbk"))

    archive = tmp_path / "t.7z"
    subprocess.run(["7zz", "a", "-bso0", "-bsp0", str(archive), f"{DAY:%Y%m%d}"], cwd=src.parent.parent, check=True)
    ingest(DAY, archive, root)

    store = RawStore(root, ledger)
    df_o = store.inspect_raw("orders", DAY, ("column_6", "column_9", "column_10", "column_8"))
    assert df_o.row(0) == ("18446744073709551615", "1.2e5", "\x00", "B")
    for c in df_o.columns:
        assert df_o.schema[c] == pl.String

    df_t = store.inspect_raw("trades", DAY, ("column_6", "column_7"))
    assert df_t.row(0) == ("0", "\x00")


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_impl_ingest_header_only_orders_produces_consistent_columns(tmp_path, root, ledger):
    """独立事实：HEADER['orders'] = "万得代码,...,委托数量,"（11 个值，尾随逗号代表空字段）；
    body 非空时 split(',') 出 11 字段。所以**正常摄取** parquet schema必有 column_1..column_11 + _symbol
    （共 12 列）。空 CSV 走 _column_count 的 header.rstrip(',').split(',') 路径会得 10，
    导致列数与非空摄取不一致 —— 这是已识别的实现 bug。
    攻：_column_count 头-only 分支。"""
    src = tmp_path / "src" / f"{DAY:%Y%m%d}" / "000001.SZ"
    src.mkdir(parents=True)
    # orders 仅头（0 行）
    (src / CSV_NAME["orders"]).write_bytes((HEADER["orders"] + "\n").encode("gbk"))
    # trades/xinqing 各 1 行（让三流齐全）
    trade_line = "000001.SZ,000001,20220104,093000000,1,0,0,B,100000,100,0,0,"
    (src / CSV_NAME["trades"]).write_bytes((HEADER["trades"] + "\n" + trade_line + "\n").encode("gbk"))
    xinqing_line = "000001.SZ,000001,20220104,093000000," + ",".join(["100"] * 63)
    (src / CSV_NAME["xinqing"]).write_bytes((HEADER["xinqing"] + "\n" + xinqing_line + "\n").encode("gbk"))

    archive = tmp_path / "t.7z"
    subprocess.run(["7zz", "a", "-bso0", "-bsp0", str(archive), f"{DAY:%Y%m%d}"], cwd=src.parent.parent, check=True)
    ingest(DAY, archive, root)

    orders_schema = pq.read_schema(root / "orders" / f"date={DAY:%Y%m%d}.parquet")
    expected_names = [f"column_{i}" for i in range(1, NCOLS["orders"] + 1)] + ["_symbol"]
    assert orders_schema.names == expected_names, (
        f"orders 列集合不一致：期望 {expected_names}，实际 {orders_schema.names}"
    )


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_impl_ingest_empty_csv_writes_valid_zero_row_parquet(tmp_path, root, ledger):
    """独立事实：pyarrow.write_table 对0 行 large_string表格会写出 num_rows=0、
    num_row_groups ∈ {0, 1} 的合法 parquet（独立测表：见 tmp脚本）。
    攻：_write_parquet 对0 行 frame 的产出。"""
    src = tmp_path / "src" / f"{DAY:%Y%m%d}" / "000001.SZ"
    src.mkdir(parents=True)
    for s in STREAMS:
        (src / CSV_NAME[s]).write_bytes((HEADER[s] + "\n").encode("gbk"))
    archive = tmp_path / "t.7z"
    subprocess.run(["7zz", "a", "-bso0", "-bsp0", str(archive), f"{DAY:%Y%m%d}"], cwd=src.parent.parent, check=True)
    ingest(DAY, archive, root)
    for s in STREAMS:
        meta = pq.read_metadata(root / s / f"date={DAY:%Y%m%d}.parquet")
        assert meta.num_rows == 0
        assert meta.num_row_groups in (0, 1)
        assert meta.num_columns == NCOLS[s] + 1


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_impl_ingest_leftover_tmp_cleaned(tmp_path, root, ledger):
    """独立事实：root 下任何时刻不得存在 *.tmp 残留（包含跨进程崩溃遗留）。
    攻：实现以 .{pid}.tmp 命名临时文件 + os.replace；本进程的 tmp 必消失，
    其他 PID 与无 PID 的 .tmp 是否被清理决定此测试通过/失败。"""
    sym_data = {"000001.SZ": {"orders": ["000001.SZ,000001,20220104,093000000,1,10,0,B,100000,100,"]}}
    archive = _build_archive(tmp_path, DAY, sym_data)

    # 跨进程崩溃残留：含其他 PID 与无 PID 两种命名
    (root / "orders" / f"date={DAY:%Y%m%d}.parquet.99999.tmp").write_bytes(b"")
    (root / "orders" / f"date={DAY:%Y%m%d}.parquet.tmp").write_bytes(b"")

    ingest(DAY, archive, root)
    leftovers = list(root.rglob("*.tmp"))
    assert leftovers == [], f"摄取后仍残留 .tmp：{leftovers}"


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_impl_ingest_dropped_count_consistent(tmp_path, root, ledger):
    """独立事实：归档内标的总数 = main prefixes 标的数 + dropped_by_prefix 之和（按前缀聚合）。
    攻：_discover_csvs 的 prefix过滤与 dropped计数。"""
    sym_data = {
        "000001.SZ": {"orders": ["000001.SZ,000001,20220104,093000000,1,10,0,B,100000,100,"]},
        "300001.SZ": {"orders": ["300001.SZ,300001,20220104,093000000,2,20,0,B,200000,200,"]},
        "688001.SH": {"orders": ["688001.SH,688001,20220104,093000000,3,30,A,B,300000,300,"]},
        "830001.BJ": {"orders": ["830001.BJ,830001,20220104,093000000,4,40,0,B,400000,400,"]},
    }
    archive = _build_archive(tmp_path, DAY, sym_data)
    r = ingest(DAY, archive, root, prefixes=MAIN_PREFIXES)
    assert r.dropped_by_prefix == {"300": 1, "688": 1, "830": 1}
    assert sum(r.dropped_by_prefix.values()) + 1 == 4      # 主板 1 + 丢弃 3 = 总标的 4


@pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7zz")
def test_impl_ingest_sha256_is_content_sensitive(tmp_path, ledger):
    """独立事实：sha256 对输入字节敏感；两个字节不同的归档 ⇒ archive_sha256 不同。
    攻：_sha256_file 的字节级哈希。"""
    root1 = tmp_path / "preserve1"
    root2 = tmp_path / "preserve2"
    for r in (root1, root2):
        for s in STREAMS:
            (r / s).mkdir(parents=True)
        (r / "manifest").mkdir()

    a1 = _build_archive(tmp_path / "a1", DAY,
 {"000001.SZ": {"orders": ["000001.SZ,000001,20220104,093000000,1,10,0,B,100000,100,"]}})
    a2 = _build_archive(tmp_path / "a2", DAY,
                       {"000001.SZ": {"orders": ["000001.SZ,000001,20220104,093000000,1,10,0,B,100001,100,"]}})

    r1 = ingest(DAY, a1, root1)
    r2 = ingest(DAY, a2, root2)
    assert r1.archive_sha256 != r2.archive_sha256


# ==============================================================================
# 7) 读取路径：monkeypatch spy
# ==============================================================================


def test_impl_execute_uses_pyarrow_with_pre_buffer_true_and_no_polars_read_parquet(monkeypatch, root, ledger):
    """独立事实：实现必须只走 pyarrow 读取入口并传 pre_buffer=True；禁止 pl.read_parquet / pl.scan_parquet。
    攻：_read_file_plan / inspect_raw 的 IO 入口调用与 kwargs。"""
    import pyarrow.parquet as _pq
    import polars as _pl

    write_preserve(root, "orders", DAY, [order_row("000001.SZ", "093000000", oid="1", price="100000")])
    store = RawStore(root, ledger)
    req = ReadRequest("orders", (DAY,), ("oid", "price"))
    p = plan(req, store.catalog("orders", (DAY,)), ledger)

    pf_calls: list[dict] = []
    rt_calls: list[dict] = []
    real_pf = _pq.ParquetFile
    real_rt = _pq.read_table

    class SpyPF:
        def __init__(self, *args, **kwargs):
            pf_calls.append({"args": args, "kwargs": dict(kwargs)})
            self._real = real_pf(*args, **kwargs)

        def read_row_groups(self, *a, **kw):
            return self._real.read_row_groups(*a, **kw)

    def spy_rt(*args, **kwargs):
        rt_calls.append({"args": args, "kwargs": dict(kwargs)})
        return real_rt(*args, **kwargs)

    monkeypatch.setattr(_pq, "ParquetFile", SpyPF)
    monkeypatch.setattr(_pq, "read_table", spy_rt)

    pl_rp_calls: list[dict] = []
    pl_scan_calls: list[dict] = []
    real_pl_rp = _pl.read_parquet
    real_pl_scan = _pl.scan_parquet

    def spy_pl_rp(*a, **kw):
        pl_rp_calls.append({"args": a, "kwargs": dict(kw)})
        return real_pl_rp(*a, **kw)

    def spy_pl_scan(*a, **kw):
        pl_scan_calls.append({"args": a, "kwargs": dict(kw)})
        return real_pl_scan(*a, **kw)

    monkeypatch.setattr(_pl, "read_parquet", spy_pl_rp)
    monkeypatch.setattr(_pl, "scan_parquet", spy_pl_scan)

    # execute 路径
    res = store.execute(p)
    assert res.frame.height == 1
    # inspect_raw 路径
    df_raw = store.inspect_raw("orders", DAY, ("column_4",))
    assert df_raw.height == 1

    # 期望：pyarrow 路径被使用，且 ParquetFile 调用带 pre_buffer=True
    assert pf_calls, "ParquetFile 未被调用"
    assert all(c["kwargs"].get("pre_buffer") is True for c in pf_calls), pf_calls
    # inspect_raw 也走 pyarrow.read_table
    assert rt_calls, "pyarrow.read_table 未被调用"
    assert all(c["kwargs"].get("pre_buffer") is True for c in rt_calls), rt_calls

    # 期望：polars 读取入口一次都不被调用
    assert pl_rp_calls == [], f"pl.read_parquet 被调用：{pl_rp_calls}"
    assert pl_scan_calls == [], f"pl.scan_parquet 被调用：{pl_scan_calls}"


# ==============================================================================
# 8) quality / inspect_raw / 重复 days
# ==============================================================================


def test_impl_quality_unknown_value_raises(root, ledger):
    """独立事实：Quality 枚举是合法值集合；manifest 中 quality 不在枚举内 ⇒失败（fail-loud）。
    攻：quality() 的 Quality(value) 转换与异常路径。"""
    write_preserve(root, "orders", DAY, [])
    write_preserve(root, "trades", DAY, [])
    write_preserve(root, "xinqing", DAY, [])
    manifest = root / "manifest" / f"{DAY:%Y%m%d}.json"
    manifest.write_text('{"quality": "bogus_value"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="bogus_value"):
        RawStore(root, ledger).quality(DAY)


def test_impl_inspect_raw_with_symbols_filter_returns_subset_rows(root, ledger):
    """独立事实：inspect_raw 给 symbols 时按 _symbol 过滤；返回行数 = 该 stream 中匹配标的的行数。
    攻：inspect_raw 的 filter 路径与 select(columns) 丢弃 _symbol。"""
    rows = [order_row(s, "093000000", oid=str(i))
            for i, s in enumerate(["000001.SZ", "000002.SZ", "000003.SZ", "600000.SH"])]
    write_preserve(root, "orders", DAY, rows)

    store = RawStore(root, ledger)
    df = store.inspect_raw("orders", DAY, ("column_4",), symbols=frozenset({"000001.SZ", "600000.SH"}))
    assert df.height == 2
    assert df.columns == ["column_4"]

    df_all = store.inspect_raw("orders", DAY, ("column_4",), symbols=None)
    assert df_all.height == 4


def test_impl_inspect_raw_does_not_apply_decode(root, ledger):
    """独立事实：inspect_raw 是旁路，不走 schema、不走 decode；dtype 全 pl.String；哨兵原样保留。
    攻：_store_impl.inspect_raw 不调用 to_int64 / to_time_ms。"""
    write_preserve(root, "orders", DAY, [
        order_row("000001.SZ", "093000000", oid="18446744073709551615", price="1.2e5", vol="\x00", side=" "),
    ])
    df = RawStore(root, ledger).inspect_raw("orders", DAY, ("column_6", "column_9", "column_10", "column_8"))
    assert df.row(0) == ("18446744073709551615", "1.2e5", "\x00", " ")
    for c in df.columns:
        assert df.schema[c] == pl.String


def test_impl_execute_request_with_duplicate_days_raises_at_construction():
    """独立事实：ReadRequest 构造即校验；days 有重复 ⇒ ValueError（不在 execute 层）。
    攻：ReadRequest.__post_init__ 的去重校验。"""
    with pytest.raises(ValueError, match="重复"):
        ReadRequest("orders", (DAY, DAY), ("oid",))
    with pytest.raises(ValueError, match="重复"):
        ReadRequest("orders", (DAY, DAY, DAY), ("oid",))
