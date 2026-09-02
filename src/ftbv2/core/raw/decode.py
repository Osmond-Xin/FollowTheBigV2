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
    |x| > 2^53                  → null          （Float64 中转会丢精度；V1 会静默给错值，V2 判定为 null。
                                                  判定函数：int(float(s)) 与 int(s) 不相等即 null。实测语料里没有这种值）

时间：九位 HHMMSSmmm → (h*3600+m*60+s)*1000+mmm；六位 HHMMSS 与五位 HMMSS（同一现象：厂商省掉了毫秒与前导零）→ 秒 ×1000，
**只在缺陷账本登记 time_6digit 的天允许**；其他长度 → null；h>23 或 m>59 或 s>59 或 mmm>999 → null。
归一化按行、按字符串长度（去掉首尾空白后），因为同一文件里混着两种。
"""

from __future__ import annotations

import polars as pl

from ftbv2.core.raw.types import Window


def to_int64(column: str) -> pl.Expr:
    """字符串列 → Int64，遵守模块 docstring 里的边界表。"""
    raw = pl.col(column)
    as_float = raw.cast(pl.Float64, strict=False)
    candidate = as_float.cast(pl.Int64, strict=False)
    exact_int = raw.cast(pl.Int64, strict=False)
    integer_like = raw.str.contains(r"^[+-]?\d+$")
    loses_integer_precision = integer_like & (exact_int.is_null() | (candidate != exact_int))
    return pl.when(loses_integer_precision).then(None).otherwise(candidate)


def to_time_ms(column: str, *, allow_6digit: bool) -> pl.Expr:
    """时间字符串列 → 自午夜起毫秒 Int64。allow_6digit=False 时六位值 → null（调用方须先检查并硬失败）。"""
    raw = pl.col(column).str.strip_chars()
    length = raw.str.len_chars()

    def parse_ms(hour: pl.Expr, minute: pl.Expr, second: pl.Expr, millis: pl.Expr) -> pl.Expr:
        valid = (
            hour.is_between(0, 23)
            & minute.is_between(0, 59)
            & second.is_between(0, 59)
            & millis.is_between(0, 999)
        )
        value = ((hour * 3600 + minute * 60 + second) * 1000 + millis).cast(pl.Int64)
        return pl.when(valid).then(value).otherwise(None)

    nine = parse_ms(
        raw.str.slice(0, 2).cast(pl.Int64, strict=False),
        raw.str.slice(2, 2).cast(pl.Int64, strict=False),
        raw.str.slice(4, 2).cast(pl.Int64, strict=False),
        raw.str.slice(6, 3).cast(pl.Int64, strict=False),
    )
    six = parse_ms(
        raw.str.slice(0, 2).cast(pl.Int64, strict=False),
        raw.str.slice(2, 2).cast(pl.Int64, strict=False),
        raw.str.slice(4, 2).cast(pl.Int64, strict=False),
        pl.lit(0),
    )
    five = parse_ms(
        raw.str.slice(0, 1).cast(pl.Int64, strict=False),
        raw.str.slice(1, 2).cast(pl.Int64, strict=False),
        raw.str.slice(3, 2).cast(pl.Int64, strict=False),
        pl.lit(0),
    )
    return (
        pl.when(length == 9)
        .then(nine)
        .when((length == 6) & pl.lit(allow_6digit))
        .then(six)
        .when((length == 5) & pl.lit(allow_6digit))
        .then(five)
        .otherwise(None)
    )


def time_digit_lengths(column: str) -> pl.Expr:
    """该列去空白后的字符串长度集合（用于检测未登记的六位时间：长度 ≤ 7 即污染）。返回 List[UInt32] 或等价。"""
    return pl.col(column).str.strip_chars().str.len_chars().drop_nulls().unique().sort()


def in_windows(time_ms_column: str, windows: tuple[Window, ...]) -> pl.Expr:
    """time_ms 落在任一 [start_ms, end_ms) 内的布尔表达式。"""
    expr = pl.lit(False)
    for window in windows:
        expr = expr | (
            (pl.col(time_ms_column) >= window.start_ms)
            & (pl.col(time_ms_column) < window.end_ms)
        )
    return expr
