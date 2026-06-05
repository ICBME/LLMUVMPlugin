# LLM Plugin Codegen Architecture

本文档描述 reference model / scoreboard 生成和验证流程。直接 Python plugin 路径仍
不引入通用 DSL；OracleIR 是当前已接入的受限 reference model DSL。该流程只作为
目标侧产物生成与验证工具，不改变 replay core 的职责边界。

OracleIR ref model 是同一验证/接入链路上的受限 DSL 变体：先生成结构化
`OracleIR`，再由 codegen 生成 Python `GeneratedOracleRefModel` bundle，并复用
candidate validation、golden case validation 和 final artifact promotion。
当前链路评估见 [Reference Model OracleIR 评估](ref_model_oracle_ir_eval.md)。

## 目标

- 在 IR 已生成之后，让 LLM 基于 spec、manifest、IR 和插件契约生成初版
  reference model / scoreboard Python plugin。
- 生成结果先进入 candidate 目录，经过静态检查、插件契约检查和可选 golden case
  检查。
- 验证通过后再提升为 final artifact，并由 target manifest 指向最终 plugin。

## 非目标

- 直接 Python plugin 路径不实现通用协议 DSL 或 scoreboard DSL；当前只有 OracleIR
  这一条受限 reference model DSL。
- 不让 LLM 修改 `libafl_bfm_fuzz` replay core。
- 不让未验证的 LLM 代码直接进入 final artifact 或 manifest。
- 不在 IR 中表达协议预测规则、scoreboard 策略或 timing 行为。

## 数据流

```text
spec / RTL docs / register docs
              |
              v
        generated BFM IR
              |
              v
  rtlagent-codegen write-prompt
              |
              v
        LLM file bundle JSON
              |
              v
 candidate artifacts directory
              |
              v
 static + contract + golden validation
              |
              v
 final artifacts directory
              |
              v
 target manifest ref_model / scoreboard / bfm_ir
              |
              v
 existing pyUVM replay flow
```

## 产物包格式

LLM 返回 strict JSON：

```json
{
  "files": [
    {
      "path": "generated/my_dut_ref_model.py",
      "content": "..."
    },
    {
      "path": "generated/my_dut_scoreboard.py",
      "content": "..."
    }
  ],
  "assumptions": [
    "Expected value is computed from the documented register mirror."
  ],
  "required_tests": [
    "Run directed read-after-write golden cases before promotion."
  ],
  "metadata": {
    "ref_model": "generated.my_dut_ref_model:MyRefModel",
    "scoreboard": "generated.my_dut_scoreboard:MyScoreboard"
  }
}
```

约束：

- `path` 必须是相对路径，不能包含 `..`。
- `content` 必须是文本。
- candidate 目录可保留失败产物用于 debug。
- final 目录只接收验证通过的候选产物。

## 验证门槛

`rtlagent_bfm.codegen.validation` 当前提供三层检查：

1. 静态检查：Python 语法、AST 解析、禁止明显危险的进程/网络/文件访问入口。
2. 插件契约检查：ref model 必须有 `predict(case)`；scoreboard 必须有
   `write(record)`、`check()`、`summary()`，且 `summary()` 返回 dict。
3. Golden case 检查：可选地调用 ref model 的 `predict(case)`，比较返回的
   `expected` 与 directed expected。

这些检查只能证明候选产物满足初步接入条件，不能替代完整 replay、coverage 和人工
review。复杂状态机、乱序响应、多周期 monitor-driven scoreboard 应在后续 DSL 或更强
验证能力出现后再自动化。

## CLI 使用

生成 prompt：

```sh
uv run python -m rtlagent_bfm.codegen.cli write-prompt \
  --manifest /path/to/target.toml \
  --ir /path/to/generated_ir.json \
  --spec /path/to/spec.md \
  --out generated/candidates/run_001/prompt.json
```

写入 candidate：

```sh
uv run python -m rtlagent_bfm.codegen.cli write-candidate \
  --bundle generated/candidates/run_001/llm_bundle.json \
  --candidate-dir generated/candidates/run_001/artifacts
```

验证 candidate：

```sh
uv run python -m rtlagent_bfm.codegen.cli validate \
  --artifact-dir generated/candidates/run_001/artifacts \
  --target my_dut \
  --ref-model generated.my_dut_ref_model:MyRefModel \
  --scoreboard generated.my_dut_scoreboard:MyScoreboard \
  --python-path libafl_bfm_fuzz/py \
  --golden-cases tests/my_dut_golden.json
```

验证、提升并更新 manifest：

```sh
uv run python -m rtlagent_bfm.codegen.cli finalize \
  --bundle generated/candidates/run_001/llm_bundle.json \
  --candidate-dir generated/candidates/run_001/artifacts \
  --final-dir generated/final \
  --target my_dut \
  --ref-model generated.my_dut_ref_model:MyRefModel \
  --scoreboard generated.my_dut_scoreboard:MyScoreboard \
  --manifest /path/to/target.toml \
  --bfm-ir generated/final/my_dut_ir.json \
  --python-path libafl_bfm_fuzz/py \
  --golden-cases tests/my_dut_golden.json
```

OracleIR ref model 路径：

```sh
uv run python -m rtlagent_bfm.codegen.cli generate-oracle-ir \
  --manifest /path/to/target.toml \
  --spec /path/to/spec.md \
  --target my_dut \
  --out generated/candidates/run_001/oracle_ir.json

uv run python -m rtlagent_bfm.codegen.cli validate-oracle-ir \
  --oracle-ir generated/candidates/run_001/oracle_ir.json \
  --manifest /path/to/target.toml \
  --target my_dut \
  --require-rules \
  --golden-cases tests/my_dut_golden.json

uv run python -m rtlagent_bfm.codegen.cli generate-oracle-plugins \
  --oracle-ir generated/candidates/run_001/oracle_ir.json \
  --target my_dut \
  --out-bundle generated/candidates/run_001/oracle_bundle.json
```

## 与现有框架的关系

- `rtlagent_bfm` IR 仍只描述语义信号和 HDL path 绑定。
- `libafl_bfm_fuzz` replay 仍只通过 manifest 加载 plugin。
- 生成的 ref model 和 scoreboard 与手写 plugin 使用同一契约。
- 产物提升后，manifest 中的 `bfm_ir`、`ref_model`、`scoreboard` 指向 final artifact。

## OracleIR 链路状态

OracleIR 路径当前已接入 `example/verilog-eval` smoke 测试脚本：

```sh
uv run python libafl_bfm_fuzz/scripts/verilog_eval_oracle_chain_test.py \
  --limit -1 \
  --max-input-bits 8
```

该脚本使用 Verilator 从 VerilogEval `RefModule` 生成 golden truth table，再生成
OracleIR ref model bundle，并通过 `finalize_bundle()` 验证和提升。2026-06-04 的
结果是在当前 stateless 可表示集合上 54/54 通过；完整数据集覆盖 54/156。主要限制是
truth-table 状态空间、latch/时序状态和 `x/z` 四值逻辑。
