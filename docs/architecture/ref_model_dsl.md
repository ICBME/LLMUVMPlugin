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
Spec2Backend/Proof/
  model.py        # proof backend protocol/result/obligation models
  plan.py         # temporary proof obligation planning and normalization
  lean4.py        # opt-in Lean4 backend
  wrapper.py      # deterministic wrapper template checker
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

### Lean4 proof backend

`verify_ref_model_ir()` 和通用 `run_checks()` 支持显式启用 Lean4 proof pass。默认验证流程仍只使用
schema/reference/type/extern/Z3；调用方必须传入 `proof_backend="lean4"` 并让 passes 包含
`"proof"`，才会启动 Lean。

Lean4 v2 使用 checker 内部的临时 proof plan，不新增持久 IR 层。默认
`proof_scope=("expr",)`，保持只证明 `RefModelPlan` 与 `RefModelIR` 的纯表达式等价；
调用方可传 `proof_options={"proof_scope": ("expr", "rule", "step")}` 追加规则等价和一步
state update 等价证明。`proof_options["max_subgoals"]` 默认 64，超过后返回 blocking
`proof_subgoal_limit` issue。

增强后的 proof plan 继续保持 opt-in，并新增两个显式 scope：

- `semantic`: 从 `SemanticSpecIR` 的 RepresentationAST、`RefModelPlan.rules` 和
  `RefModelIRAdapter` 的 equivalence context 构造 `semantic_plan_equiv`、
  `plan_ir_expr_equiv`、`plan_ir_step_equiv` obligation。调用方可以通过
  `proof_options["semantic_ir"]` 和 `proof_options["ref_model_plan"]` 传入语义来源；
  adapter metadata 中已有 plan 时也可复用。第一版证明的是 per-rule/per-step expression
  等价，metadata 明确记录 obligation kind 和 normalized hash；完整多规则优先级、totality
  和 overlap 仍由 SMT pass 负责。
- `implementation`: 运行 `wrapper_template_check`，输入为
  `proof_options["wrapper_source"]` 或 `proof_options["wrapper_path"]`。该检查只接受
  `emit_ref_model_plugin()` 生成的 deterministic wrapper 形状：`__init__` 必须通过
  `RefModelInterpreter.from_path(...)` 构造 interpreter 并初始化 state，`predict()` 只能调用
  interpreter `step()`，再把 interpreter outputs 包装成 `ExpectedResult`。该 scope 不启动
  Lean，metadata 标注 `implementation_boundary="wrapper_template_only"` 和
  `trusted_runtime="RefModelInterpreter"`。

`rule` scope 第一版证明的是按 `source_rule_id`/`id` 匹配后的 condition-aware per-rule
表达式等价；规则 totality、overlap 和多规则覆盖关系仍由 SMT pass 负责。`step` scope 针对
`step_rules` 中的一步 state update obligation，不做无界 temporal proof。

- 支持 `bool`、`int`、`uint`、已知宽度 `bitvector(width)` 和 choices 唯一的 `enum`。
- BitVec 支持算术/位运算以及 fixed-width `concat`、`slice`、`reduce_and`、`reduce_or`、
  `reduce_xor`。
- `mux`/`if` 会在 proof plan 中拆成 bounded 子目标，condition 会提升为 theorem assumption。
- `invariant` scope 已注册但第一版不实现 preservation proof；显式请求会返回
  `invariant_unimplemented` blocking issue。
- 不支持 `any`、`string`、`bytes`、未知 extern、trusted extern、无宽度 bitvector、复杂 operation
  或 temporal/SVA 义务；这些情况返回 blocking `proof` issue。
- `semantic` scope 支持 `assignment`、`constant_relation`、`conditional_assignment`、
  `compare`、`unary_op`、`binary_op`、`mux`、`concat`、`slice`、`reduce`、fixed-width
  BitVec、Bool、Int、UInt 和 enum 子集；temporal/protocol AST 第一版只做结构/类型检查，
  请求 kernel proof 时返回 blocking `unsupported_semantic_obligation`。
- Bool 义务使用 case split/simp；BitVec 义务使用 Lean `Std.Tactic.BVDecide`；Int/UInt 只使用
  Lean/Std 可用的 `simp`/`rfl` 风格证明，不依赖 mathlib。
- Lean proof source 不落盘，report metadata 记录 backend、Lean version、obligation id、theorem
  SHA256、normalized obligation SHA256、obligation kind、subgoal count、axioms、proved obligations
  和 unsupported obligations。静态 wrapper 检查结果记录在 `metadata.proof.static_checks`。

证明通过必须满足两个条件：Lean kernel 接受 theorem，且 `#print axioms` 中不存在未批准 axiom。
实现禁止生成或接受 `sorry`、`admit`、`axiom`、`unsafe`。Lean 4.31 的 `bv_decide` 会报告
`propext`、`Classical.choice`、`Quot.sound` 以及 theorem-local native certificate axiom；这些会被
显式记录在 metadata 中，仅作为该 tactic 的批准内建依赖处理。

### RefModelPlan lowering provenance

`build_ref_model_plan()` 生成的 rule 保持 schema_version 兼容，并为 proof backend 附加
非持久 provenance 字段：

- `semantic_element_id`
- `source_ast_node`
- `source_ast_hash`
- `lowering_kind`
- `lowered_rule_hash`

这些字段只进入 proof metadata 和审计链路，不写回 `SemanticSpecIR`，也不代表 backend
readiness。hash 变化会改变 proof obligation 的 normalized hash，使语义 lowering 变更能被
review 和 CI 捕获。

### Implementation correctness boundary

当前 implementation scope 的结论是“生成 wrapper 未越过固定模板边界”，不是“任意 Python
实现已经被 Lean 证明”。`RefModelInterpreter` 暂作为小可信运行时；若需要提升该边界，后续应
引入 Lean semantics 与 interpreter differential/exhaustive tests，或把 interpreter 语义本身形式化。

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
