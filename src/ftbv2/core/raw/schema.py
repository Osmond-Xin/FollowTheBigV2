"""原始层 schema：column_N ↔ 语义、dtype 还原种类、会话常量、宇宙前缀。

这是整个系统的公理（数据表第三节）：表头在 V1 摄取时被丢掉，没有真值源。两条独立依据——
幸存 7z 的真实 GBK 表头逐字段对照、V1 设计文档——结论一致。V2 摄取把表头原文写进 manifest，
让公理逐步变成可查的数据。

来源：docs/数据表.html 第三节；V1 src/followthebig/data/preserve_schema.py；V1 utils/constants.py。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Stream = Literal["orders", "trades", "xinqing"]
STREAMS: tuple[Stream, ...] = ("orders", "trades", "xinqing")

# 7z 内每个标的目录下的三个 CSV 文件名 ↔ stream
CSV_NAME: dict[Stream, str] = {"xinqing": "行情.csv", "orders": "逐笔委托.csv", "trades": "逐笔成交.csv"}

SYMBOL_COL = "_symbol"          # preserve 文件里标识行所属标的的列，值如 "002783.SZ"
PRICE_SCALE = 10_000            # 价格 = 元 × 10000 整数定点（数据表第三节；20220104 茅台 ask1 锚点）
ROW_GROUP_ROWS = 122_880        # 与现有 preserve 文件一致（数据表第二节实测），单标的一天只碰约 2 个 row group

# 标的宇宙不在这里：样本宇宙属于预注册（词汇表）。摄取保留归档里的全部标的；V1 按主板前缀删行是 F182 模式，V2 不重复。

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
        *_ob("ask_px", 18), *_ob("ask_sz", 28), *_ob("bid_px", 38), *_ob("bid_sz", 48),
    ),
}

def field(stream: Stream, name: str) -> Field:
    """按语义名取字段；未登记名抛 KeyError（不是静默返回 None）。未登记列只能经 RawStore.inspect_raw 给人看。"""
    for f in FIELDS[stream]:
        if f.name == name:
            return f
    raise KeyError(f"{stream} 没有字段 {name!r}")
