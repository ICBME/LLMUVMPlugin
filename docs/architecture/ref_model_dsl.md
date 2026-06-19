# RefModelIR/DSL 架构

`Spec2Backend/RefModelDSL` 提供 IR-first 的 reference model 生成、执行和验证链路。
它把 LLM 的职责从“直接生成可 import 的 Python 插件”收窄为“生成可校验的
RefModelIR JSON”，再由框架确定性生成 UVM plugin wrapper。

## 目标

- 最终仍满足现有 UVM contract：manifest 指向 `module:Object`，对象实现
  `predict(case) -> ExpectedResult`。
- LLM 输出必须是 strict JSON IR，不直接输出 Python wrapper。
- 可形式化的 `RefModelPlan` 规则必须通过 Z3 obligation 验证。
- 标准参考实现可以标记为 `trusted_standard`，报告为 trusted，不宣称 SMT 证明。
- 非标准高计算量外部实现必须提供 DSL `formal_model`，否则不能提升为 verified final。

## 目录

```text
Spec2Backend/Checks/
  model.py        # shared check issue/report/type/symbol models
  engine.py       # generic schema/reference/type/extern/SMT pass runner
  adapters.py     # RefModelIRAdapter and SemanticSpecIRAdapter
Spec2Backend/RefModelDSL/
  schema.py        # RefModelIR schema helpers and verification report objects
  interpreter.py   # DSL interpreter used by generated wrapper
  extern.py        # python_stdlib and c_abi runtime extern registry
  verifier.py      # schema/type/extern/Z3 verification
  emit_plugin.py   # deterministic UVM wrapper emitter
  codegen.py       # IR-first feedback codegen task
```

## RefModelIR

顶层 IR 是 JSON object：

```json
{
  "schema_version": 1,
  "target": "notgate",
  "inputs": {
    "value": {"type": "bool"}
  },
  "outputs": {
    "expected": {"type": "bool"}
  },
  "rules": [
    {
      "id": "ref_rule_1",
      "source_rule_id": "ref_rule_1",
      "assign": {
        "expected": {"not": {"field": "value"}}
      }
    }
  ],
  "externs": {},
  "verification": {"required": true},
  "metadata": {}
}
```

表达式支持当前 v1 用例所需的稳定子集：

- `literal`、`field`、`state`
- `if`、`mux`、`match`
- `eq`、`compare`
- `not`、`and`、`or`
- `binary` / `binary_op`: `add`、`sub`、`mul`、`and`、`or`、`xor`
- `decode_hex`、`encode_hex`
- `concat`、`slice`、`cast`
- `extern_call`

状态类 ref model 可以声明 `state`、`init_rules` 和 `step_rules`。v1 interpreter 支持
one-step state update；verifier 只做 one-step transition equivalence，不做无界 temporal proof。

## 反馈生成入口

推荐入口：

```python
from Spec2Backend.RefModelDSL import generate_ref_model_ir_with_feedback

result = generate_ref_model_ir_with_feedback(
    ref_model_plan,
    manifest_path="targets/demo.toml",
    output_dir="generated/ref_model_ir/demo",
    llm_backend=backend,
    golden_cases=golden_cases,
    max_attempts=3,
)
```

LLM 返回：

```json
{
  "ref_model_ir": {"schema_version": 1, "target": "demo"},
  "metadata": {},
  "assumptions": []
}
```

每轮 attempt 写出：

- `prompt.json`
- `llm_response.json`
- `artifacts/ref_model_ir.json`
- `artifacts/verification_report.json`
- `evaluation.json`
- `feedback.json`

成功后 `output_dir/final` 包含：

- `ref_model_ir.json`
- `verification_report.json`
- `generated/<target>_dsl_ref_model.py`
- `bundle.json`

`bundle.json` 的 `metadata.ref_model` 指向框架生成的 deterministic wrapper，而不是
LLM metadata 中的任意对象。

## Interpreter 与 UVM Wrapper

`RefModelInterpreter(ir, base_dir)` 执行 DSL，并返回 JSON-safe expected value。
生成 wrapper 形如：

```python
class DslRefModel:
    def predict(self, case):
        value = RefModelInterpreter.from_path("ref_model_ir.json").eval(case.data)
        return ExpectedResult(expected=value)
```

实际文件由 `emit_ref_model_plugin()` 生成，路径是
`final/generated/<target>_dsl_ref_model.py`。调用方可通过现有
`GeneratedPluginBundle`、`validate_generated_plugin_bundle()`、
`build_plugin_registry()` 和 manifest overlay 接入 replay。

## Extern Policy

所有 extern 都必须声明 `pure=true` 和 `deterministic=true`。

`python_stdlib` v1 allowlist：

- `hashlib.sha224`
- `hashlib.sha256`
- `hashlib.sha384`
- `hashlib.sha512`

`python_stdlib` 必须使用 `verification_policy="trusted_standard"`，并提供
`standard_name`、`implementation`、`version` 和 `artifact_sha256`。verifier 记录为
`trusted_standard_externs`，不对标准算法本身做 SMT 等价证明。

`c_abi` 使用 `ctypes.CDLL` 调用 candidate artifact 目录内的相对路径 shared library。
schema/verifier 要求：

- `library` 是相对 POSIX 路径，不能包含 `..`。
- `function`、`arg_codecs`、`return_codec` 明确。
- `artifact_sha256` 和实际文件 hash 一致。
- `timeout_ms` 是正整数。
- `verification_policy="formal_model"`。
- `formal_model` 是 DSL 表达式，Z3 使用该模型证明 RefModelPlan obligation。
- `conformance_cases` 用 runtime C ABI 调用检查样例一致性。

当前 v1 的 C ABI runtime 是同进程 `ctypes` 调用，`timeout_ms` 已作为 schema 要求记录，
但尚未通过隔离 worker 强制中断长调用。高风险 C/C++ extern 应在后续接入独立 worker。

`systemc_worker` 仅保留 schema 方向；v1 verifier 会给出 blocking issue。

## Verifier

`verify_ref_model_ir()` 输出稳定 JSON report，并保持旧 public API。内部通过
`Spec2Backend.Checks.RefModelIRAdapter` 调用通用 checker，再把 `CheckReport` 映射回
`VerificationReport`；C ABI runtime conformance 仍在 RefModelDSL 侧执行，因为它依赖
`ExternRegistry`。

```json
{
  "status": "passed",
  "verification_level": "formally_verified",
  "proved_rules": ["ref_rule_1"],
  "trusted_standard_externs": [],
  "tested_externs": [],
  "blocked_issues": []
}
```

验证层包括：

- schema/reference/type 检查：target、inputs、outputs、rules、field/state/extern 引用、
  bool condition、assignment compatibility 和 bitvector width。
- rule totality：每个 output 在所有输入域上必须至少被一条 rule 赋值。
- rule overlap：同一 output 的多条 rule 不能在同一输入条件下同时赋值。
- extern policy：allowlist、路径、hash、formal model、conformance。
- RefModelPlan equivalence：支持 `assignment`、`constant_relation`、
  `conditional_assignment`、`operation_relation` 以及 one-step transition 的 v1 子集。

如果 `z3-solver` 不可 import，IR-first final promotion 会失败，并返回 blocking issue。

## 与 Legacy Python File-Bundle 路径的关系

`Spec2Backend.FeedbackCodegen.generate_ref_model_with_feedback()` 仍保留，适合作为 legacy
或快速 fallback。可信推荐路径是
`Spec2Backend.RefModelDSL.generate_ref_model_ir_with_feedback()`：

- LLM 只生成 IR。
- wrapper 由框架生成。
- 可形式化规则进入 Z3。
- 外部高计算量实现必须显式说明 trust/proof 边界。

## 当前边界

- verifier 覆盖 RefModelPlan 的 v1 可表达子集；复杂协议和无界时序仍需人工或后续 proof。
- 标准参考实现依赖 provenance 和 golden/property conformance，不宣称 SMT 证明标准库。
- C ABI 只能证明 DSL `formal_model` 与计划等价，不能自动证明任意二进制实现等价于
  `formal_model`。
- SystemC worker 尚未实现。
