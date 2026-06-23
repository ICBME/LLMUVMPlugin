# Spec2Backend Feedback Codegen Architecture

本文档描述新的反馈闭环 LLM 代码生成链路。旧 `rtlagent_bfm.codegen`
实现已经移除；新的实现目录是 `Spec2Backend/FeedbackCodegen`，首版只提供
Python API，不提供 CLI。

可信推荐路径是 `Spec2Backend/RefModelDSL` 的 IR-first 入口：LLM 生成
`RefModelIR`，框架再确定性生成 UVM wrapper 并用 Z3 验证可形式化规则。本文档中的
Python file-bundle 路径仍保留为 legacy/fallback。

## 目标

- 从 `RefModelPlan` 生成 reference model candidate。
- 推荐模式生成可验证 `RefModelIR`，legacy 模式生成 Python plugin file bundle。
- 每轮生成都经过 candidate 目录、静态检查、插件契约检查和 golden case 检查。
- 将失败诊断压缩成结构化 feedback，进入下一轮 LLM prompt。
- 使用 `ConnectGraph`/`PipelineOrchestrator` 记录 prompt、LLM response、candidate、
  evaluation、feedback 和 final artifact 的 connector 事件。
- final promotion 只写入 `output_dir/final`，不自动修改 target manifest。

## 数据流

```text
SemanticSpecIR
      |
      v
BackendReadiness
      |
      v
RefModelPlan
      |
      v
Spec2Backend.RefModelDSL prompt
      |
      v
LLM strict JSON RefModelIR
      |
      v
attempt_XXX/artifacts/ref_model_ir.json
      |
      v
generic CheckReport schema/type/extern/Z3 verification
      |
      v
optional Lean4 proof pass for RefModelPlan/RefModelIR equivalence
      |
      v
deterministic UVM wrapper + golden evaluation
      |
      +--> structured verification feedback -> next attempt
      |
      v
output_dir/final
```

Lean4 proof pass 是显式 opt-in。启用后，feedback 会包含 `proof` stage 的 blocking issues、
Lean theorem hash、axiom report 和 failed obligation id。LLM 可以根据这些结构化诊断修复
`RefModelIR` candidate，但不能自行声明 proof 成功；只有 Lean kernel 接受且 axiom policy 通过的
obligation 才会进入 `proved_rules` / `metadata.proof.proved_obligations`。

## Python API

```python
from LLMPlugin import create_backend
from Spec2Backend.RefModelDSL import generate_ref_model_ir_with_feedback

backend = create_backend("langgraph", model="...")

result = generate_ref_model_ir_with_feedback(
    ref_model_plan,
    manifest_path="targets/demo.toml",
    spec_paths=("specs/demo.md",),
    output_dir="generated/ref_model_codegen/demo",
    llm_backend=backend,
    golden_cases=(
        {
            "case_id": "case_1",
            "target": "demo",
            "data": {"target": "demo", "value": 7},
            "expected": 7,
        },
    ),
    max_attempts=3,
)
```

`result.status` 的主要值：

- `succeeded`
- `llm_unavailable`
- `llm_invalid_response`
- `invalid_bundle`
- `max_attempts_exhausted`

## RefModelIR Contract

IR-first 路径要求 LLM 返回：

```json
{
  "ref_model_ir": {
    "schema_version": 1,
    "target": "demo",
    "inputs": {"value": {"type": "int"}},
    "outputs": {"expected": {"type": "int"}},
    "rules": [
      {
        "id": "ref_rule_1",
        "source_rule_id": "ref_rule_1",
        "assign": {"expected": {"field": "value"}}
      }
    ],
    "externs": {},
    "verification": {"required": true},
    "metadata": {}
  },
  "metadata": {},
  "assumptions": []
}
```

成功 promotion 会写出 `final/ref_model_ir.json`、`final/verification_report.json`、
`final/generated/<target>_dsl_ref_model.py` 和 `final/bundle.json`。`bundle.json`
中的 `metadata.ref_model` 由框架生成，不信任 LLM 覆盖。

更多 schema、extern policy 和 verification level 见
[RefModelIR/DSL 架构](ref_model_dsl.md)。

RefModelIR-first 路径的验证内部复用 `Spec2Backend.Checks`。反馈环路消费的仍是兼容
`VerificationReport` JSON，但其 blocking issues 来自统一 `CheckReport` pass，包括
schema/reference/type/extern/SMT 检查。

## Legacy Bundle Contract

LLM 返回 strict JSON：

```json
{
  "files": [
    {
      "path": "generated/demo_ref_model.py",
      "content": "from fuzz_uvm.contracts import ExpectedResult\n..."
    }
  ],
  "metadata": {
    "ref_model": "generated.demo_ref_model:GeneratedRefModel"
  },
  "assumptions": [
    "Expected value follows RefModelPlan rule ref_rule_1."
  ]
}
```

约束：

- `files[].path` 必须是相对 POSIX 路径，不能是绝对路径，不能包含 `..`。
- `files[].content` 必须是文本。
- `metadata.ref_model` 必须是 `module:Object`，指向 candidate 目录中的 plugin class。
- 生成代码不能使用 `subprocess`、`socket`、`requests`、`urllib`、`eval`、`exec`、
  `__import__`、`importlib`、`os.system` 或文件写入 API。

## Legacy Evaluation

Python file-bundle fallback 的 evaluation 分两层：

- 主进程静态检查：`ast.parse`、危险 import/API、明显路径写入。
- 子进程 contract/golden 检查：临时把 candidate artifact 目录加入 `sys.path`，
  import `metadata.ref_model`，调用 `fuzz_uvm.contracts.validate_ref_model_plugin()`，
  再用 golden case 调用 `predict(case)` 并比较 `ExpectedResult.expected`。

子进程 worker 是 `Spec2Backend.FeedbackCodegen.ref_model_eval_worker`。这样每轮
candidate 的 import、`sys.path` 和 `sys.modules` 状态不会污染主进程。

Evaluation issue 使用稳定 JSON：

```json
{
  "stage": "golden_case",
  "severity": "error",
  "case_id": "case_1",
  "message": "expected value mismatch",
  "expected": 7,
  "actual": 0,
  "blocking": true
}
```

## ConnectGraph Topology

`FEEDBACK_CODEGEN_TOPOLOGY` 包含以下 connector：

- `plan_to_prompt`
- `prompt_to_llm_response`
- `response_to_candidate`
- `candidate_to_evaluation`
- `evaluation_to_feedback`
- `candidate_to_final_artifact`

完整 prompt、response、bundle、evaluation 和 feedback 都写入每个
`attempt_XXX` 目录；connector event 只记录 artifact refs 和 metrics。

## 当前边界

- 首版只实现 ref model adapter；可信推荐入口是 RefModelIR/DSL。
- OracleIR、scoreboard、SVA、test-vector backend 后续通过同一个 task protocol 接入。
- 不自动修改 manifest；调用方负责 review final artifact 并决定是否接入 replay。
- 不默认运行 replay/simulation；需要时应新增 evaluator plugin。
