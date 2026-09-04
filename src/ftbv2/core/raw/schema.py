"""原始层 schema：column_N ↔ 语义、dtype 还原种类、会话常量、宇宙前缀。

这是整个系统的公理（数据表第三节）：表头在 V1 摄取时被丢掉，没有真值源。两条独立依据——
幸存 7z 的真实 GBK 表头逐字段对照、V1 设计文档——结论一致。V2 摄取把表头原文写进 manifest，
让公理逐步变成可查的数据。

来源：docs/数据表.html 第三节；V1 src/followthebig/data/preserve_schema.py；V1 utils/constants.py。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal

Stream = Literal["orders", "trades", "xinqing"]
STREAMS: tuple[Stream, ...] = ("orders", "trades", "xinqing")

# 7z 内每个标的目录下的三个 CSV 文件名 ↔ stream
CSV_NAME: dict[Stream, str] = {"xinqing": "行情.csv", "orders": "逐笔委托.csv", "trades": "逐笔成交.csv"}

SYMBOL_COL = "_symbol"          # preserve 文件里标识行所属标的的列，值如 "002783.SZ"
PRICE_SCALE = 10_000            # 价格 = 元 × 10000 整数定点（数据表第三节；20220104 茅台 ask1 锚点）

LOT_SIZE = 100
"""一手 = 100 股：**交易所定的最小委托单位**，不是我们选的一个数。

与 `PRICE_SCALE`、十档发布范围同类——都是市场的结构常数，不是参数。
买入委托必须是它的整数倍；卖出允许零股（不足一手的余数），那也是「再切不下去」的一端。

它的用途是让「一手不能再切，所以一手的重复挂单不是切片的证据」成为一句**结构**判断
而不是一个幅度阈值（2026-09-03 用户裁定，见 design-log 冰山那篇）。
写在这里而不是写在用它的地方：口径单源，谁要用谁来取。"""
ROW_GROUP_ROWS = 122_880        # 与现有 preserve 文件一致（数据表第二节实测），单标的一天只碰约 2 个 row group

# 摄取的前缀筛选（立项讨论 Q15；2026-09-02 用户裁定沿用 V1）：只存主板。这是「不得在未声明样本宇宙前删除行」
# 规则的**显式例外**，登记在词汇表「样本宇宙」条与数据表第四节；除此之外原始层不删任何行。
MAIN_PREFIXES: tuple[str, ...] = ("000", "001", "002", "003", "600", "601", "603", "605")

# 连续竞价时段（毫秒自午夜起）。V1 constants: T_AM_START=93000000 等 HHMMSSmmm 整数，这里换算成 ms
AM_START_MS = (9 * 3600 + 30 * 60) * 1000      # 09:30:00.000
AM_END_MS = (11 * 3600 + 30 * 60) * 1000       # 11:30:00.000
PM_START_MS = 13 * 3600 * 1000                 # 13:00:00.000
PM_END_MS = (14 * 3600 + 57 * 60) * 1000       # 14:57:00.000 收盘集合竞价开始 = 连续竞价 PM 结束

Kind = Literal["symbol", "time", "int", "price", "enum", "str"]
"""dtype 还原种类：
- symbol：字符串，前导零是代码的一部分，永不转数值；
- time：HHMMSSmmm 九位 / HHMMSS 六位字符串 → 自午夜起毫秒 Int64（六位只在缺陷账本登记的天允许）；
- int / price：字符串 → Int64，经 Float64 中转，逐位复刻 V1 `_i64`（见 decode.py 的边界表）；
- enum：保持字符串 / dictionary，值域不手写、未知值原样保留（枚举漂移：2025 起多出 ' ' C I J O S）；
- str：原样字符串，NUL 字节 '\\x00' 保留为 '\\x00'，它不是 null 也不是空串。
"""


@dataclass(frozen=True)
class Field:
    name: str        # 语义名，调用者用它
    column: str      # 物理列 column_N（或 _symbol）
    kind: Kind


def _ob(prefix: str, first: int) -> tuple[Field, ...]:
    kind: Kind = "price" if prefix.endswith("px") else "int"
    return tuple(Field(f"{prefix}_{k}", f"column_{first + k - 1}", kind) for k in range(1, 11))


FIELDS: dict[Stream, tuple[Field, ...]] = {
    "orders": (
        Field("symbol", SYMBOL_COL, "symbol"),
        Field("time_ms", "column_4", "time"),
        Field("oid", "column_6", "int"),          # 交易所委托号
        Field("type", "column_7", "enum"),        # SZ 0/1/U 订单类型；SH A=新增 D=删除；2025 起多出 ' ' S
        Field("side", "column_8", "enum"),        # B/S；2025 起多出 ' ' C I J O
        Field("price", "column_9", "price"),
        Field("vol", "column_10", "int"),
    ),
    "trades": (
        Field("symbol", SYMBOL_COL, "symbol"),
        Field("time_ms", "column_4", "time"),
        Field("seq", "column_5", "int"),          # 成交编号；7 天整列空 + 10 天稀疏重复（缺陷账本）
        Field("code", "column_6", "enum"),        # SZ 0=成交 C=撤单；SH 全部 '\x00'
        Field("bs", "column_8", "enum"),          # B/S；SZ 撤单行是 ' '，"U" 在数据里不存在
        Field("price", "column_9", "price"),
        Field("vol", "column_10", "int"),
        Field("ask_ref", "column_11", "int"),
        Field("bid_ref", "column_12", "int"),
    ),
    "xinqing": (
        Field("symbol", SYMBOL_COL, "symbol"),
        Field("time_ms", "column_4", "time"),
        Field("last_price", "column_5", "price"),
        # 以下九列 2026-09-03 按 V2 摄取 manifest 里的 CSV 表头原文登记（万得行情：成交量,成交额,成交笔数,…,当日累计成交量,当日成交额,最高价,最低价,开盘价,前收盘,…,叫卖总量,叫买总量）
        Field("tick_vol", "column_6", "int"),        # 本帧成交量（股）
        Field("tick_amt", "column_7", "int"),        # 本帧成交额（元）
        Field("n_trades", "column_8", "int"),        # 成交笔数（累计）
        Field("cum_vol", "column_12", "int"),        # 当日累计成交量（股）
        Field("cum_amt", "column_13", "int"),        # 当日成交额（元）；超 int32，必须 int64
        Field("high", "column_14", "price"),
        Field("low", "column_15", "price"),
        Field("open", "column_16", "price"),
        Field("prev_close", "column_17", "price"),
        *_ob("ask_px", 18), *_ob("ask_sz", 28), *_ob("bid_px", 38), *_ob("bid_sz", 48),
        Field("ask_total", "column_60", "int"),      # 叫卖总量（全簿，不止十档）
        Field("bid_total", "column_61", "int"),      # 叫买总量
    ),
}

ARCHIVE_NAME_RE = re.compile(r"^(\d{8})\.7z$")


def archive_day(name: str) -> "dt.date | None":
    """规范归档文件名 YYYYMMDD.7z → 交易日；`20260303(1).7z` 重复件、`*.downloading` 半成品等一律 None（调用方登记，不静默）。"""
    m = ARCHIVE_NAME_RE.match(name)
    if m is None:
        return None
    try:
        return dt.date(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:]))
    except ValueError:
        return None


def parquet_relpath(stream: Stream, day: "dt.date") -> str:
    """{root} 下 preserve 文件的相对路径。布局是接口不变量，store 与 ingest 都从这里派生。"""
    return f"{stream}/date={day:%Y%m%d}.parquet"


def manifest_relpath(day: "dt.date") -> str:
    return f"manifest/{day:%Y%m%d}.json"


def field(stream: Stream, name: str) -> Field:
    """按语义名取字段；未登记名抛 KeyError（不是静默返回 None）。未登记列只能经 RawStore.inspect_raw 给人看。"""
    for f in FIELDS[stream]:
        if f.name == name:
            return f
    raise KeyError(f"{stream} 没有字段 {name!r}")
