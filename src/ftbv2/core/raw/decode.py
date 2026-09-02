"""dtype 还原：字符串 → 语义 dtype 的纯 polars 表达式。实现 owner 填空；契约见 tests/raw。

不变量（数据表第三节「类型转换的既定行为」，必须逐位复刻 V1 `_i64`，否则下游 >0 / 时段过滤会变且极难察觉）：

    输入                        → Int64 输出
    ''                          → null          （不是 0）
    ' '                         → null          （SZ 撤单行 BS 位）
    '\\x00'                      → null          （SH trades c6/c7；字符串路径保留原值，数值路径 null）
    '18446744073709551615'      → null          （UINT64 哨兵，经 Float64 中转后落 null）
    '1.2018e+006'               → 1201800       （科学计数法被解析）
    'abc'                       → null
    '2151938037'                → 2151938037    （超 int32，必须 int64）

时间：九位 HHMMSSmmm → (h*3600+m*60+s)*1000+mmm；六位 HHMMSS（或五位 HMMSS）→ 秒 ×1000，
**只在缺陷账本登记 time_6digit 的天允许**；其他长度 → null。归一化按行、按字符串长度，因为同一文件里混着两种。
"""

from __future__ import annotations

import polars as pl

from ftbv2.core.raw.types import Window


def to_int64(column: str) -> pl.Expr:
    """字符串列 → Int64，遵守模块 docstring 里的边界表。"""
    raise NotImplementedError


def to_time_ms(column: str, *, allow_6digit: bool) -> pl.Expr:
    """时间字符串列 → 自午夜起毫秒 Int64。allow_6digit=False 时六位值 → null（调用方须先检查并硬失败）。"""
    raise NotImplementedError


def time_digit_lengths(column: str) -> pl.Expr:
    """该列去空白后的字符串长度集合（用于检测未登记的六位时间：长度 ≤ 7 即污染）。返回 List[UInt32] 或等价。"""
    raise NotImplementedError


def in_windows(time_ms_column: str, windows: tuple[Window, ...]) -> pl.Expr:
    """time_ms 落在任一 [start_ms, end_ms) 内的布尔表达式。"""
    raise NotImplementedError
