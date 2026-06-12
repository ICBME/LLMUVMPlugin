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

也可以使用 LLM/codegen 流程生成 ref model / scoreboard 初版：

1. 使用已生成 IR、manifest 和 spec 生成 LLM prompt。
2. 将 LLM 返回的 file bundle 写入 candidate 目录。
3. 运行静态、插件契约和 golden case 验证。
4. 验证通过后提升到 final 目录，并更新 manifest 中的 `bfm_ir`、`ref_model` 和
   `scoreboard`。

详见 [LLM Plugin Codegen 架构](../architecture/llm_plugin_codegen.md)。

如果 ref model 能表达为 deterministic、stateless 的输入到输出预测，也可以优先尝试
OracleIR 路径：生成或手写 `OracleIR`，验证 golden cases，然后生成
`GeneratedOracleRefModel` bundle。当前链路和边界见
[Reference Model OracleIR 评估](../architecture/ref_model_oracle_ir_eval.md)。

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

该目标会通过 `scripts/run_fuzz_pipeline.py generate-corpus` 串联 Rust corpus
generator 和 Python corpus validation。若需要指定 cargo toolchain 或 wrapper，可传入
quoted `CARGO`，例如 `CARGO='cargo +nightly'`。

## 7. Replay

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz \
  TARGET=my_dut \
  TARGET_CONFIG=/path/to/my_dut.toml \
  EXTRA_PYTHONPATH=/path/to/plugin/python \
  generate-corpus

UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz \
  TARGET=my_dut \
  TARGET_CONFIG=/path/to/my_dut.toml \
  VERILOG_SOURCES="/path/to/rtl/a.v /path/to/rtl/b.v" \
  TOPLEVEL=my_dut_top \
  EXTRA_PYTHONPATH=/path/to/plugin/python \
  sim
```

`sim` 只负责 replay 已存在的 corpus；`coverage-run`、`feedback-fuzz` 和
`feedback-campaign` 会由 orchestrator 自动生成并校验各自需要的 corpus。

## 8. 可选：打开 Connector 观测

任意 Makefile 目标都可以设置 connector 输出，用于观察 corpus generation、
validation、replay、scoreboard、functional coverage 和 feedback 各层：

```sh
CONNECTOR_OBSERVE_OUT=/path/to/events.jsonl
CONNECTOR_MONITOR_OUT=/path/to/monitor.json
CONNECTOR_TOPOLOGY_OUT=/path/to/topology.json
CONNECTOR_OBSERVE_RUN_ID=my_dut_smoke
```

例如：

```sh
uv run make -C libafl_bfm_fuzz \
  TARGET=my_dut \
  TARGET_CONFIG=/path/to/my_dut.toml \
  EXTRA_PYTHONPATH=/path/to/plugin/python \
  CONNECTOR_OBSERVE_OUT=/tmp/my_dut_events.jsonl \
  CONNECTOR_MONITOR_OUT=/tmp/my_dut_monitor.json \
  CONNECTOR_TOPOLOGY_OUT=/tmp/my_dut_topology.json \
  CONNECTOR_OBSERVE_RUN_ID=my_dut_smoke \
  CONNECTOR_OBSERVE_ROUND_ID=round_00 \
  generate-corpus

uv run make -C libafl_bfm_fuzz \
  TARGET=my_dut \
  TARGET_CONFIG=/path/to/my_dut.toml \
  VERILOG_SOURCES="/path/to/rtl/a.v /path/to/rtl/b.v" \
  TOPLEVEL=my_dut_top \
  CONNECTOR_OBSERVE_OUT=/tmp/my_dut_events.jsonl \
  CONNECTOR_MONITOR_OUT=/tmp/my_dut_monitor.json \
  CONNECTOR_TOPOLOGY_OUT=/tmp/my_dut_topology.json \
  CONNECTOR_OBSERVE_RUN_ID=my_dut_smoke \
  CONNECTOR_OBSERVE_ROUND_ID=round_00 \
  sim
```

详细事件 schema 和 connector 列表见
[Connector Observability 架构](../architecture/connector_observability.md)。

## 9. Coverage Feedback

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
- `<target>_feedback_corpus.jsonl`
- `<target>_round_manifest.json`
- `<target>_campaign/campaign_manifest.json`（使用 `feedback-campaign` 时）

如果需要自定义功能覆盖率输出路径，可设置：

```sh
UVM_FUNCTIONAL_COVERAGE_OUT=/path/to/my_dut_functional.json
```

默认 coverage model 会统计 manifest 字段、可选 `[[coverpoint]]` 和 `[[cross]]`。
如果覆盖点需要 DUT response、错误类型或协议状态机上下文，应提供目标专用
`coverage_model` plugin，并可实现 `sample_record(record)`。

## 10. 可选：Harness Optimization Plugin

如果新 DUT 的剩余 RTL gap 需要目标专用解释，或需要新的 safe action surface，不要把规则写进
framework 核心。提供一个 harness optimization plugin，并在 target manifest 中声明：

```toml
[harness_optimization]
plugins = ["my_project.eval_plugin:MyHarnessOptimizationPlugin"]
```

plugin 可以返回或注册：

- `HarnessActionPlugin`：声明 `action_type`、safe sandbox 标记、payload DSL、validator、
  adapter artifact kind/env var，以及该 action 是否属于 runtime action。
- gap actionability classifier：把 coverage summary 中的 top gaps 转换成
  `actionability`、`recommended_action_type` 和 `suggested_payload`。

真实 candidate regression 会把这些插件同时用于 LLM schema hint、sandbox apply、
candidate adapter config、per-action attribution 和 `candidate_gap_actionability_report`。
CLI 也可临时传入：

```sh
uv run make -C libafl_bfm_fuzz \
  TARGET=my_dut \
  TARGET_CONFIG=/path/to/my_dut.toml \
  HARNESS_OPTIMIZATION_PLUGINS=my_project.eval_plugin:MyHarnessOptimizationPlugin \
  real-candidate-evidence-campaign
```

LLM 应只选择已注册 action 与 payload；新插件代码本身应先走 sandbox validation 和
promotion review，而不是让 LLM 直接修改主线代码。

## 11. 保持目标代码外置

新目标的专用文件应保存在目标工程或插件目录中，不放进可复用框架核心。仓库内
`fuzz_examples` 和 `targets/secworks_*` 只作为 smoke/example target：

- driver plugin
- ref model
- scoreboard
- coverage model
- target manifest
- RTL source list
- directed tests 或 vectors
