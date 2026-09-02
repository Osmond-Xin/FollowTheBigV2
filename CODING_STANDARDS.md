# 编码标准（code-review skill 的 Standards 轴依据）

术语以 `CONTEXT.md` 为准；模块与接口以 `docs/架构图.html` 为准；数据以 `docs/数据表.html` 为准。以下只写代码层面的硬规则，
全部对应 V1 的真实事故（立项讨论第三节），门禁能机械判定的不在这里重复（见 `tools/gate.sh`）。

## 结构
- 纯逻辑核 `ftbv2.core` 不触碰 IO；IO 层 `ftbv2.io` 逻辑尽量薄，主要职责是异常处理与资源管理。
- 接口先于实现：模块的公开面 = 类型 + docstring 里的不变量、顺序约束、错误模式、性能特征。实现不得扩大公开面。
- 不为单适配器引入 port；不为「以后可能」加参数。接口上没有 `force` / `ignore_missing` / `relax` 一类旋钮。
- 私有符号（`_x`）不跨模块 import；同一常量全仓只出现一次，从 `ftbv2.core.raw.schema` 一类单源 import。

## 失败方式
- fail-loud：盘未挂载、未登记的数据形状、行数不符、判定不可用——一律抛 `RuntimeError` 并说明天与 stream，绝不返回空结果或打印后继续。
- 缺口是数据，不是异常：请求得到的缺失以 `Gap` 携带归因码返回；「查不到 = 没有」被禁止。
- 不吞异常：`except Exception` 只允许出现在门禁脚本把故障转成非零退出的地方。

## 数据语义
- 价格是元 × 10000 整数定点，全程 Int64，不转浮点。
- 字符串 → 数值只经 `ftbv2.core.raw.decode`，逐位复刻 V1 `_i64` 边界表；时间归一化按行、按字符串长度。
- 枚举不手写映射：保持字符串 / dictionary，未知值原样保留。`'\x00'`、`''`、`' '` 是三个不同的值。
- 行序 = 文件序；任何产物不得在未声明样本宇宙前删除行（摄取的前缀筛选是决策 Q15，并计数上报）。

## 读取路径
- 只走 `pyarrow.parquet.read_table(..., pre_buffer=True)` → `polars.from_arrow`；禁止 `pl.read_parquet(use_pyarrow=True)`（慢 13 倍）。
- 按 `_symbol` 的 row group statistics 裁剪；时间窗永远不下推，只在扫描后过滤。
- 读的字节数与 row group 数来自 footer 元数据并写进 `ReadStats`，让加速可观测。

## 写入路径
- 原子写：先写临时文件再 `os.replace`；幂等判据是 manifest 完整，不是「某个文件存在」。
- 行数校验必须独立计数（CSV 字节流换行数），不得从 parquet 反推。
- 输出与现有 preserve 逐位兼容：`column_1..N` 全 large_string + `_symbol`，按 `_symbol` 升序，标的内保持原序，`row_group_size = schema.ROW_GROUP_ROWS`。

## 测试
- 契约测试只通过接口观察行为，不 mock 内部；夹具与真实文件同形（`tests/raw/conftest.py`）。
- 期望值来自独立事实（数据表、实测表），不得由被测代码同样的算法算出。
