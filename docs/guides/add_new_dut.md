# Add a New DUT

本文档给出新 DUT 接入建议步骤。

## 1. 准备输入材料

- RTL source list。
- Top-level module 名。
- 时钟和 reset 名称。
- 协议文档、寄存器描述和测试需求。
- 可选 reference model 或 golden model。

## 2. 生成或编写 IR

如果 driver 需要通过语义名访问 DUT signal，先生成 `rtlagent_bfm` IR。

IR 应记录：

- `design.top`
- `interfaces`
- `bindings`
- `sources`
- 可选 `registers`

## 3. 编写 Driver Plugin

driver plugin 必须实现：

```python
async def reset(self) -> None:
    ...

async def execute(self, case) -> ReplayResult:
    ...
```

建议：

- 使用 manifest `[signals]` 或 `bfm_ir` 获取 DUT signal。
- 将协议 timing 留在 driver 内，不放进框架核心。
- `detail` 字段写入便于 debug 的 transaction 摘要。

## 4. 编写可选插件

按需要添加：

- ref model plugin。
- scoreboard plugin。
- functional coverage plugin。

如果没有 expected，但只想 smoke replay，请使用自定义 scoreboard，默认 scoreboard
会要求 expected 存在。

## 5. 编写 Target Manifest

最小结构：

```toml
name = "my_dut"
toplevel = "my_dut_top"
driver = "my_project.my_driver:MyDriver"

[[field]]
name = "op"
kind = "enum"
choices = ["read", "write"]
```

## 6. 生成 Corpus

```sh
make -C libafl_bfm_fuzz \
  TARGET=my_dut \
  TARGET_CONFIG=/path/to/my_dut.toml \
  generate-corpus
```

## 7. Replay

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz \
  TARGET=my_dut \
  TARGET_CONFIG=/path/to/my_dut.toml \
  VERILOG_SOURCES="/path/to/rtl/a.v /path/to/rtl/b.v" \
  TOPLEVEL=my_dut_top \
  EXTRA_PYTHONPATH=/path/to/plugin/python \
  sim
```

## 8. Coverage Feedback

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz \
  TARGET=my_dut \
  TARGET_CONFIG=/path/to/my_dut.toml \
  VERILOG_SOURCES="/path/to/rtl/a.v /path/to/rtl/b.v" \
  TOPLEVEL=my_dut_top \
  feedback-fuzz
```

检查输出：

- `<target>_coverage_summary.json`
- `<target>_uvm_functional_coverage.json`
- `<target>_mutation_directives.json`

## 9. 保持目标代码外置

目标专用文件应保存在目标工程或插件目录中，不放进框架核心：

- driver plugin
- ref model
- scoreboard
- coverage model
- target manifest
- RTL source list
- directed tests 或 vectors
