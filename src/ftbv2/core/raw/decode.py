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
    精度丢失                    → null          （Float64 中转会丢精度；V1 会静默给错值，V2 判定为 null。
                                                  判定函数就是规则本身：int(float(s)) != int(s) 即 null。
                                                  因此 2^53 = 9007199254740992 保留，2^53+1 → null。实测语料里没有这种值）

时间：九位 HHMMSSmmm 与**八位 HMMSSmmm**（10 点前小时不足两位时厂商省掉前导零，如 93000123 = 09:30:00.123；
每天约 28% 的行是八位——2026-09-02 在 2022-01-27 三条流实测，数据表第四节检测子一栏「合法只有 8 或 9」早已写明，
但第一版实现只认九位，把整个早盘静默置 null）→ (h*3600+m*60+s)*1000+mmm；
六位 HHMMSS 与五位 HMMSS（同一现象：省掉毫秒后再省前导零，如 84500 = 08:45:00；秒位从不省略）→ 秒 ×1000，
**只在缺陷账本登记 time_6digit 的天允许**；其他长度 → null；h>23 或 m>59 或 s>59 或 mmm>999 → null；
首字符不是数字（含 +/- 号）→ null。归一化按行、按字符串长度（去掉首尾空白后），因为同一文件里混着两种。
拆位用整数算术而不是字符串切片（同一串按数值拆 h/m/s/ms 与按位置切在纯数字串上等价，快 40%，且八位九位共用一条公式）。

性能（2026-09-02 在 9 千万行的真实 orders 列上实测）：polars 不会合并表达式树里重复的 strip_chars，每个引用都重算一次
（to_int64 引用 2 次、to_time_ms 引用 12 次），正则 ^[+-]?\\d+$ 再花 0.8 s。所以 store 先把要还原的列 strip **物化一次**，
再以 pre_stripped=True 调用；整数判定用「Int64 直接解析非空」代替正则——两者对边界表逐项等价（超过 Int64 的整数串
经 Float64 中转本来就落 null）。单列 5.1 s → 1.75 s。
"""

from __future__ import annotations

import polars as pl

from ftbv2.core.raw.schema import Field, Kind
from ftbv2.core.raw.types import Window


def _raw(column: str, pre_stripped: bool) -> pl.Expr:
    return pl.col(column) if pre_stripped else pl.col(column).str.strip_chars()


def to_int64(column: str, *, pre_stripped: bool = False) -> pl.Expr:
    """字符串列 → Int64，遵守模块 docstring 里的边界表。pre_stripped=True 表示调用方已去掉首尾空白（store 物化一次）。"""
    raw = _raw(column, pre_stripped)             # 带首尾空白的超精度整数串不能逃过精度判定（复审建议）
    candidate = raw.cast(pl.Float64, strict=False).cast(pl.Int64, strict=False)
    exact_int = raw.cast(pl.Int64, strict=False)   # 非空 ⇔ 整数串且在 Int64 范围内；超范围的整数串 candidate 本来就是 null
    loses_integer_precision = exact_int.is_not_null() & (candidate != exact_int)
    return pl.when(loses_integer_precision).then(None).otherwise(candidate)


def to_time_ms(column: str, *, allow_6digit: bool, pre_stripped: bool = False) -> pl.Expr:
    """时间字符串列 → 自午夜起毫秒 Int64。allow_6digit=False 时六位值 → null（调用方须先检查并硬失败）。"""
    raw = _raw(column, pre_stripped)
    length = raw.str.len_chars()
    digits = raw.str.slice(0, 1).cast(pl.UInt8, strict=False).is_not_null()   # 首字符是数字：排除 +/- 号
    n = raw.cast(pl.Int64, strict=False)                                       # 非纯数字串（小数点、字母）⇒ null

    def parse_ms(hour: pl.Expr, minute: pl.Expr, second: pl.Expr, millis: pl.Expr) -> pl.Expr:
        valid = (
            hour.is_between(0, 23)
            & minute.is_between(0, 59)
            & second.is_between(0, 59)
            & millis.is_between(0, 999)
        )
        value = ((hour * 3600 + minute * 60 + second) * 1000 + millis).cast(pl.Int64)
        return pl.when(valid).then(value).otherwise(None)

    with_millis = parse_ms(n // 10_000_000, n // 100_000 % 100, n // 1_000 % 100, n % 1_000)   # 九位 HHMMSSmmm / 八位 HMMSSmmm
    no_millis = parse_ms(n // 10_000, n // 100 % 100, n % 100, pl.lit(0))                        # 六位 HHMMSS / 五位 HMMSS
    return (
        pl.when(digits & length.is_between(8, 9))
        .then(with_millis)
        .when(digits & length.is_between(5, 6) & pl.lit(allow_6digit))
        .then(no_millis)
        .otherwise(None)
    )


def in_windows(time_ms_column: str, windows: tuple[Window, ...]) -> pl.Expr:
    """time_ms 落在任一 [start_ms, end_ms) 内的布尔表达式。"""
    expr = pl.col(time_ms_column) < 0          # 逐行的恒假基底：空窗返回每行 False，不是一个标量
    for window in windows:
        expr = expr | (
            (pl.col(time_ms_column) >= window.start_ms)
            & (pl.col(time_ms_column) < window.end_ms)
        )
    return expr


def short_time_present(column: str, *, pre_stripped: bool = False) -> pl.Expr:
    """去空白后是 5 或 6 位纯数字的时间值是否存在（未登记 time_6digit 的天出现即硬失败）。
    空串、7 位等其他形状不是这个缺陷：to_time_ms 会把它们置 null，不在这里误归因。"""
    return _raw(column, pre_stripped).str.contains(r"^\d{5,6}$").fill_null(False).any()


def decode_field(f: Field, *, allow_6digit: bool, pre_stripped: bool = False) -> pl.Expr:
    """按 schema.Kind 把物理列还原成语义列（不含别名）。"""
    if f.kind == "time":
        return to_time_ms(f.column, allow_6digit=allow_6digit, pre_stripped=pre_stripped)
    if f.kind in ("int", "price"):
        return to_int64(f.column, pre_stripped=pre_stripped)
    return pl.col(f.column)


def strip_columns(f: Field) -> bool:
    """哪些物理列需要去首尾空白后再还原（time / int / price）；store 对投影里的这些列物化一次 strip。"""
    return f.kind in ("time", "int", "price")


def output_dtype(kind: Kind) -> pl.DataType:
    """语义列的输出 dtype：time / int / price → Int64，其余保持字符串。"""
    return pl.Int64() if kind in ("time", "int", "price") else pl.String()
