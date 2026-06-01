# Target Manifest Reference

Target manifest 是接入 DUT 的核心配置文件。Rust corpus generator、Python
corpus validator 和 pyUVM replay 都依赖同一份 manifest。

## 基本示例

```toml
name = "my_dut"
toplevel = "my_dut_top"
clock = "clk"
clock_period_ns = 1.0
reset = "reset_n"
driver = "my_project.my_driver:MyDriver"
ref_model = "my_project.my_ref_model:MyRefModel"
scoreboard = "fuzz_uvm.scoreboards:ResultScoreboard"
coverage_model = "my_project.coverage:MyCoverageModel"

[signals]
clk = "clk"
reset_n = "reset_n"

[[field]]
name = "op"
kind = "enum"
choices = ["read", "write"]

[[field]]
name = "addr"
kind = "int"
min = 0
max = 4095

[[field]]
name = "payload"
kind = "hex"
hex_len = 16
```

## 必填项

- `name`：目标名。JSONL `target` 字段应与该名称一致。
- `driver`：目标 replay driver plugin，格式为 `module:Class`。
- 至少一个 `[[field]]`：Rust corpus generator 需要字段 schema。

## 可选项

- `toplevel`：DUT top module 名。
- `clock`：clock signal 名，默认 `clk`。
- `clock_period_ns`：clock period，默认 `1.0`。
- `reset`：reset signal 名，默认 `reset_n`。
- `[signals]`：插件可用的 signal 映射。
- `ref_model` / `oracle`：reference model plugin。
- `scoreboard`：scoreboard plugin。
- `coverage_model`：functional coverage plugin。
- `bfm_ir`：指向 `rtlagent_bfm` IR 文件。
- `sequence_schema`：保留给 sequence plugin/schema 扩展。
- `[[coverpoint]]`：可选功能覆盖点声明，供默认 coverage model 和 feedback advisor 使用。
- `[[cross]]`：可选功能覆盖交叉声明。

## LLM 生成产物接入

第一版无 DSL codegen 流程中，LLM 生成的 ref model / scoreboard 先写入
candidate 目录。验证通过后，工具会将候选产物提升到 final 目录，并更新 manifest：

```toml
bfm_ir = "generated/final/my_dut_ir.json"
ref_model = "generated.my_dut_ref_model:MyRefModel"
scoreboard = "generated.my_dut_scoreboard:MyScoreboard"
```

这些字段与手写 plugin 使用同一加载路径。未验证的 candidate artifact 不应被 manifest
引用。

## Field Schema

`int`

- 整数字段。
- 支持 `min`、`max`、`choices`。

```toml
[[field]]
name = "addr"
kind = "int"
min = 0
max = 4095
```

`enum`

- 枚举字段。
- 建议显式配置 `choices`。

```toml
[[field]]
name = "op"
kind = "enum"
choices = ["read", "write"]
```

`hex`

- 十六进制 byte string。
- 支持固定长度 `hex_len`。

```toml
[[field]]
name = "payload"
kind = "hex"
hex_len = 16
```

也支持 selector-dependent 长度 `hex_len_by`：

```toml
[[field]]
name = "size"
kind = "int"
choices = [1, 2, 4]

[[field]]
name = "payload"
kind = "hex"
hex_len_by = { size = { "1" = 1, "2" = 2, "4" = 4 } }
```

`any`

- 只做存在性约束。
- 生成器按通用文本处理。

## Functional Coverage Schema

默认 coverage model 会自动统计 manifest `[[field]]` 的字段值、相邻字段 cross，以及
可选的 `[[coverpoint]]` / `[[cross]]`。

`[[coverpoint]]` 基本格式：

```toml
[[coverpoint]]
name = "op_kind"
field = "op"
bins = ["read", "write"]
```

对 `hex` 字段可以声明 pattern bins：

```toml
[[coverpoint]]
name = "payload_pattern"
field = "payload"
patterns = ["zero", "ff", "increment", "alternating", "walking_one"]
```

`[[cross]]` 引用字段名或 coverpoint 名：

```toml
[[cross]]
name = "op_x_payload_pattern"
coverpoints = ["op", "payload_pattern"]
```

这些声明只影响 Python functional coverage 和 feedback directive 生成，不改变 Rust
corpus generator 的 schema decode 规则。为了避免过大的 summary，默认 coverage model
只会为不超过 1024 个期望组合的 cross 计算 uncovered bins。

## 路径解析

加载优先级：

1. 环境变量 `FUZZ_TARGET_CONFIG` 指向的显式 manifest。
2. 调用方传入的 `TARGET_CONFIG`。
3. `FUZZ_TARGETS_DIR/<target>.toml`。
4. 默认 `libafl_bfm_fuzz/targets/<target>.toml`。

清理后的框架不自带具体 DUT manifest。新 DUT 应显式提供 manifest。
