# Spec2IR SemanticSpecIR 设计

本文档描述当前 `Spec2Backend/Spec2IR` 的设计与实现边界。该层的核心目标是完成
自然语言 spec -> 结构化、可审查、可溯源的 `SemanticSpecIR` 迁移，并把它作为后续
ref model、SVA、OracleIR 或其他 backend artifact 的可信语义来源。

当前实现版本为 `SemanticSpecIR` schema v3。

## 设计目标

- 将自然语言硬件规格拆解为原子化、可审查的 `spec_claims`。
- 为每条语义信息保留来源文件、行号、引用文本和内容 hash，支持审计和回溯。
- 将 spec 语义表达为 backend 无关的 `semantic_elements`，不在 Spec2IR 阶段判断
  ref model、SVA、OracleIR 或其他 backend 是否可支持。
- 对缺失、歧义、冲突和未完成形式化的语义显式建模为 `open_questions` 或
  `semantic_gaps`。
- 支持 LLM 抽取、结构化 review、repair loop 和 human-in-loop 语义补全。
- 在没有 LLM 或 LLM 不可用时，提供保守的 rule-based draft，保持测试和审查链路可运行。

## 非目标

- 不生成 Python ref model、scoreboard、SVA、OracleIR 或 plugin artifact。
- 不在 `SemanticSpecIR` 中记录 backend lowering readiness、unsupported reason 或
  backend support 判断。
- 不允许未经过 schema validation、traceability review 和 completeness review 的 LLM
  输出直接成为后续可信输入。
- 不把 manifest 字段、DesignIR binding 或 source quote 作为可由 LLM 自由发明的内容。
- 不替代人工确认。对于歧义、冲突、缺失上下文或低置信度语义，Spec2IR 必须显式暴露
  human review 入口。

## 总体数据流

```text
target manifest
natural-language specs
optional DesignIR
        |
        v
build_semantic_spec_ir_prompt()
        |
        +--> LLMPlugin backend --------------+
        |                                    |
        +--> rule-based conservative draft --+
                                             |
                                             v
                         normalize_semantic_spec_ir_response()
                                             |
                                             v
                              validate_semantic_spec_ir()
                                             |
                                             v
                              review_semantic_spec_ir()
                                             |
                      +----------------------+----------------------+
                      |                                             |
                      v                                             v
        passed / accepted SemanticSpecIR            repair prompt / human input
                      |                                             |
                      v                                             |
       future ArtifactPlan / RefModelPlan / SVAPlan <---------------+
```

`SemanticSpecIR` 是规格语义层。后续产物规划应另建 `ArtifactPlan` 或类似中间层：

```text
SemanticSpecIR -> ArtifactPlan -> RefModelPlan / SVAPlan / HumanReviewPlan
```

这样可以保证 Spec2IR 只回答“spec 说了什么、证据在哪里、哪些语义尚未确定”，而不是在
抽取阶段提前绑定某个生成 backend。

## 代码结构

```text
Spec2Backend/
  Spec2IR/
    semantic_ir.py        # prompt、生成、rule-based draft、schema validation、IO
    validation_review.py  # structured review、completeness、traceability、human gate
    semantic_repair.py    # validation-feedback repair loop
LLMPlugin/
  base.py                 # provider-neutral request/response/backend protocol
  registry.py             # backend registry
  langchain_backend.py    # OpenAI-compatible LangChain backend
  langgraph_backend.py    # LangGraph wrapper
rtlagent_bfm/codegen/cli.py
                          # extract/validate/review/repair semantic IR CLI
```

### `semantic_ir.py`

主要职责：

- 加载 manifest、spec 文档和可选 DesignIR。
- 构造 strict JSON prompt。
- 调用插件化 LLM backend 或 legacy callable adapter。
- 归一化 LLM response，支持 `semantic_spec_ir`、`result`、`output` 等常见包裹形式。
- 生成 rule-based conservative draft。
- 校验 schema v3、source hash、quote、claim/evidence/element 引用关系和 manifest 字段引用。
- 读写 `SemanticSpecIR` JSON。

### `validation_review.py`

主要职责：

- 生成结构化 review report。
- 独立从 source spec 重新抽取 expected claims，不信任 IR 自带的 `spec_claims` 完整性。
- 检查 source traceability、claim coverage、semantic consistency 和 human review gate。
- 将 review status 归一为 `passed`、`failed` 或 `needs_human_input`。

### `semantic_repair.py`

主要职责：

- 根据当前 IR 和 review report 构造 repair prompt。
- 调用 LLM 修复 schema、traceability、coverage 或 consistency 问题。
- 对每次修复结果重新 review。
- 对 human-blocking 情况停止自动 repair，并返回 `needs_human_input`。

### `LLMPlugin`

主要职责：

- 提供 provider-neutral `LLMRequest` / `LLMResponse` / `LLMBackend` 协议。
- 通过 registry 支持插件化 backend 创建。
- 当前内置 `langchain` 和 `langgraph` backend。
- `langgraph` 当前是单节点 wrapper，后续可以扩展为 retry、repair、review routing 或
  human handoff graph，而不改变 Spec2IR 调用接口。

## SemanticSpecIR v3 数据模型

顶层对象必须是 JSON object，并包含以下关键字段。

### `schema_version`

当前固定为 `3`。schema 升级时必须同时更新 validator、prompt contract、tests 和本文档。

### `target`

目标 DUT 名称。必须与 manifest target 一致。

### `sources`

自然语言 spec 来源列表。每个 source 包含：

- `id`: 稳定 source id，例如 `src1`。
- `path`: source 文件路径。
- `kind`: 当前固定为 `natural_language_spec`。
- `content_hash`: source 文本 sha256。
- `line_count`: source 行数。

review 会使用传入的 `spec_paths` 重新读取 source 文件，并校验 `path` 和 `content_hash`。

### `spec_claims`

从自然语言中抽取的原子 claim。每个 claim 包含：

- `id`: 稳定 claim id，例如 `claim1`。
- `source_id`: 所属 source。
- `line_start` / `line_end`: 1-based 行号范围。
- `quote`: 来自原文行范围的短引用。
- `summary`: 归一化后的原子语义描述。
- `kind`: claim 类型。
- `strength`: 约束强度。
- `normative`: 是否为必须覆盖的规范性语义。
- `subjects`: 涉及的信号、字段、状态或协议实体。

当前允许的 claim kind：

```text
compare_policy
constraint
descriptive
functional_behavior
interface
protocol
reset
state_behavior
timing
```

当前允许的 claim strength：

```text
describes
must
shall
should
unknown
```

### `inputs`

由 manifest field 转换得到的输入摘要。当前用于让 LLM 和 validator 知道哪些字段是合法
manifest field。`representation.fields[]` 或递归对象里的 `field` 引用必须能在
manifest fields 中找到。

### `evidence`

source evidence 列表。每个 evidence 必须包含：

- `id`
- `source_id`
- `line_start`
- `line_end`
- `quote`
- `claim_ids`

validator 会检查 `quote` 必须出现在对应 source 行范围内。`semantic_elements` 必须通过
`evidence[]` 引用 evidence id。

### `semantic_elements`

backend 无关的结构化语义元素。每个 element 包含：

- `id`: 稳定 semantic element id。
- `kind`: semantic element 类型。
- `summary`: 面向审查者的语义总结。
- `formalization_status`: 形式化状态。
- `confidence`: `0.0` 到 `1.0` 的置信度。
- `subjects`: 涉及的字段、信号、状态或协议实体。
- `representation`: 结构化语义表达。
- `evidence`: evidence id 列表，不能为空。
- `claim_ids`: 被该 element 覆盖的 claim id 列表。
- `provenance`: 可选，记录抽取来源或工具信息。

当前允许的 semantic element kind：

```text
compare_policy
combinational_behavior
constraint
descriptive
example
functional_behavior
interface
protocol
reset
sequential_behavior
state_behavior
state_machine
temporal_behavior
timing
```

当前允许的 formalization status：

```text
candidate
formalized
ambiguous
incomplete
conflict
needs_human_review
```

以下状态视为 blocking formalization status。它们可以覆盖 claim，但只能作为 placeholder
coverage，review status 会进入 `needs_human_input`：

```text
ambiguous
incomplete
conflict
needs_human_review
```

### `representation`

`representation` 是 `semantic_elements` 的结构化语义主体。当前 validator 要求它至少包含：

- `type`: 非空字符串。
- `text`: 非空字符串，记录 canonical normalized semantics。

当前 rule-based draft 会根据 claim kind 生成以下 representation type：

```text
interface_decl
reset_rule
state_machine
sequential_rule
temporal_rule
protocol_rule
constraint
relation
textual_formalization
```

`representation` 是可扩展对象。当前 validator 会递归检查：

- `field`: 如果 manifest 已提供，必须是已知 manifest field。
- `fields`: 如果 manifest 已提供，列表内每个字段必须是已知 manifest field。
- `signal`、`signals`、`subjects`: 必须是字符串或字符串列表，但不强制绑定 manifest。

设计上，`representation` 表达“语义是什么”，不表达“哪个 backend 支持它”。后续可以逐步把
`type` 收紧为更强类型，例如：

```text
interface_decl
constant_relation
combinational_relation
sequential_update
reset_rule
state_machine
temporal_rule
protocol_rule
example_waveform
truth_table
```

该增强属于 SemanticSpecIR schema 的后续演进，不应在 artifact backend 内隐式实现。

### `open_questions`

human-in-loop 问题列表。典型场景：

- spec 缺少必要行为描述。
- LLM 或 rule-based extractor 无法安全判断条件、时序或状态转移。
- 多条 claim 之间存在冲突。
- 需要人工确认某个候选形式化是否完整。

blocking open question 会让 review 进入 `needs_human_input`，除非问题状态已 resolved、
closed、accepted，或 `review.human_answers` 中提供了对应回答。

### `semantic_gaps`

未形式化、歧义、冲突、缺失上下文等语义缺口。每个 gap 包含：

- `id`
- `kind`
- `reason`
- `resolution`
- `claim_ids`

当前允许的 gap kind：

```text
ambiguous
conflict
incomplete
missing_context
unformalized
```

gap 覆盖的 claim 不算完整形式化，只能说明该 claim 已被审查系统发现并等待补全。

### `review`

记录人工审查状态：

- `status`: `draft`、`needs_human_input`、`accepted` 或 `rejected`。
- `human_answers`: human-in-loop 回答。
- `reviewed_items`: 已审查的 semantic element id。

当 CLI 或 API 使用 `require_reviewed=True` 时，`review.status` 必须为 `accepted`。

### `metadata`

记录生成器、manifest、DesignIR 或其他非语义辅助信息。metadata 不应成为语义真实性来源。

## Traceability 设计

Traceability 有三条链路：

```text
source file + content_hash
        |
        v
spec_claims[source_id, line_start, line_end, quote]
        |
        v
evidence[claim_ids]
        |
        v
semantic_elements[evidence, claim_ids]
```

关键约束：

- `sources[].content_hash` 必须匹配当前 source 文件内容。
- `spec_claims[].quote` 和 `evidence[].quote` 必须出现在对应 source 行范围内。
- 每个 semantic element 必须引用至少一个 evidence id。
- 每个 semantic element 必须引用至少一个 claim id。
- review 的 completeness 阶段会从 `spec_paths` 重新抽取 expected claims，并用 source
  位置和 quote 匹配 IR claims，避免 LLM 通过删除 `spec_claims` 绕过完整性检查。

因此，`review_semantic_spec_ir()` 做可信 completeness review 时必须传入 `spec_paths`。
没有 `spec_paths` 时 review 会失败。

## Review 状态机

`review_semantic_spec_ir()` 当前包含以下 stage：

```text
schema_review
traceability_review
completeness_review
semantic_consistency_review
human_review_gate
```

### `schema_review`

调用 `collect_semantic_spec_ir_issues()`，检查：

- schema version、target、sources、claims、inputs、evidence、semantic elements、gaps 和
  review 字段结构。
- manifest target 一致性。
- manifest field 引用合法性。
- evidence、claim、element 的 id 引用合法性。

### `traceability_review`

检查：

- `spec_paths` 必须提供。
- source path 必须属于 review 输入。
- source hash 必须匹配当前文件。
- claim/evidence quote 必须能在 source 行范围中找到。
- semantic element 必须引用存在的 evidence。

### `completeness_review`

从 source spec 重新抽取 expected normative claims。每个 expected claim 必须被以下任一项覆盖：

- 完整形式化的 `semantic_elements`。
- incomplete/blocking 状态的 `semantic_elements`。
- `open_questions`。
- `semantic_gaps`。

覆盖类型决定 review 结果：

- 由完整 semantic element 覆盖：可通过 completeness。
- 只被 blocking semantic element、open question 或 semantic gap 覆盖：进入
  `needs_human_input`。
- 完全未覆盖：`failed`。

### `semantic_consistency_review`

检查：

- manifest 可加载。
- 可选 DesignIR 可加载。
- semantic representation 中的 manifest field 引用必须存在。
- `review.reviewed_items` 引用未知 semantic element 时给出 warning。

### `human_review_gate`

检查：

- `require_reviewed=True` 时，`review.status` 必须是 `accepted`。
- blocking open question 必须被关闭、解决、接受，或在 `review.human_answers` 中回答。

## Repair loop

`repair_semantic_spec_ir_with_review()` 的行为：

1. 对当前 IR 运行 review。
2. 如果 review 已 `passed`，返回 `valid`。
3. 如果 review 为 `needs_human_input`，返回 `needs_human_input`，不让 LLM 猜测人工语义。
4. 如果 review 为 `failed` 且提供 LLM backend，则构造 repair prompt。
5. LLM 返回完整修复后的 `SemanticSpecIR`。
6. 归一化、review，并按 `max_attempts` 重试。
7. 成功返回 `repaired`；LLM 不可用返回 `llm_unavailable`；响应不可解析返回
   `llm_invalid_response`；耗尽尝试返回 `repair_failed`。

repair prompt 的核心约束：

- 返回完整 IR，不返回 patch fragment。
- 保留或修复所有 source-derived normative claims。
- 不伪造 source quote。
- 不发明 manifest fields、DesignIR bindings 或 source files。
- 不在 Spec2IR 阶段生成 Python、OracleIR、SVA 或 plugin artifact。
- 无法安全形式化时，把语义留在 `semantic_gaps` 或 `open_questions`。

## LLM backend 插件化

Spec2IR 不直接依赖某个 provider。所有 LLM 调用经过 `LLMPlugin`：

```python
from LLMPlugin import create_backend

backend = create_backend("langgraph", model="...")
```

当前内置 backend：

- `langchain`: OpenAI-compatible LangChain backend，使用 `OPENAI_API_KEY`、
  `OPENAI_BASE_URL`、`OPENAI_MODEL` 等环境变量。
- `langgraph`: 默认 backend，当前包装 `langchain` delegate，为后续多节点 graph 保留扩展点。
- `CallableLLMBackend`: 测试和 legacy callable adapter。

`.env` 中的 LangSmith tracing 环境变量可由测试或命令运行前加载：

```sh
set -a
. ./.env
set +a
```

然后执行带 LLM 的抽取或修复命令。

## CLI

生成 SemanticSpecIR：

```sh
uv run python -m rtlagent_bfm.codegen.cli extract-semantic-ir \
  --manifest /path/to/target.toml \
  --spec /path/to/spec.md \
  --out generated/semantic_ir.json
```

使用 LLM backend：

```sh
uv run python -m rtlagent_bfm.codegen.cli extract-semantic-ir \
  --manifest /path/to/target.toml \
  --spec /path/to/spec.md \
  --out generated/semantic_ir.json \
  --prompt-out generated/semantic_prompt.json \
  --llm \
  --llm-backend langgraph \
  --model "$OPENAI_MODEL"
```

结构校验：

```sh
uv run python -m rtlagent_bfm.codegen.cli validate-semantic-ir \
  --semantic-ir generated/semantic_ir.json \
  --manifest /path/to/target.toml \
  --spec /path/to/spec.md \
  --target my_dut
```

结构化 review：

```sh
uv run python -m rtlagent_bfm.codegen.cli review-semantic-ir \
  --semantic-ir generated/semantic_ir.json \
  --manifest /path/to/target.toml \
  --spec /path/to/spec.md \
  --target my_dut \
  --out generated/semantic_review.json
```

repair loop：

```sh
uv run python -m rtlagent_bfm.codegen.cli repair-semantic-ir \
  --semantic-ir generated/semantic_ir.json \
  --manifest /path/to/target.toml \
  --spec /path/to/spec.md \
  --target my_dut \
  --out generated/semantic_ir.repaired.json \
  --prompt-out generated/semantic_repair_prompt.json \
  --review-out generated/semantic_repair_review.json \
  --llm \
  --llm-backend langgraph \
  --max-attempts 2
```

CLI exit code 约定：

- `0`: 通过或修复成功。
- `1`: validation/review/repair failed。
- `2`: 需要人工输入或 LLM 不可用。

## 当前测试覆盖

核心测试位于：

```text
tests/test_ref_model_codegen_semantic_ir.py
```

覆盖能力包括：

- rule-based SemanticSpecIR 生成。
- LLM response normalization。
- LLMPlugin registry、Callable backend 和 LangGraph delegate。
- CLI extract、validate、review、repair。
- source quote/hash traceability。
- completeness review 重新从 source spec 抽取 claims。
- blocking formalization status 进入 `needs_human_input`。
- `representation` 必须存在并包含结构化 `type` / `text`。
- `representation.fields[]` 必须引用 manifest fields。
- repair loop 成功、backend error 和无 LLM 情况。

推荐回归命令：

```sh
uv run python -m pytest tests/test_ref_model_codegen_semantic_ir.py -q
uv run python -m pytest tests/test_ref_model_codegen_semantic_ir.py tests/test_codegen_pipeline.py -q
uv run python -m py_compile \
  Spec2Backend/Spec2IR/semantic_ir.py \
  Spec2Backend/Spec2IR/validation_review.py \
  Spec2Backend/Spec2IR/semantic_repair.py \
  rtlagent_bfm/codegen/cli.py
git diff --check
```

## 当前能力边界

当前 `SemanticSpecIR` 已经可以作为可信审查链路的基础，但还不能声称“完全且可靠地抽取
任意 spec 的所有语义”。主要限制：

- rule-based extractor 只能生成保守 draft，真正的语义抽取依赖 LLM 或人工补全。
- `representation.type` 当前仍是开放字符串，validator 只强制 `type`、`text` 和引用合法性，
  尚未对不同 type 的内部字段做强 schema 校验。
- claim extraction 对 Markdown prose 有基本支持，但尚未覆盖表格、时序图、波形图、伪代码、
  register map、协议时序表等复杂 source 格式。
- `confidence` 当前只做范围校验，还没有基于证据强度或多轮一致性的评分策略。
- human answers 当前只作为 review gate 信号，尚未定义标准 patch/action 格式。
- optional DesignIR 当前只做可加载性检查，尚未参与 signal/port/clock/reset 层的一致性验证。

这些限制不会破坏现有审查链路，因为无法完整形式化的语义必须进入 `semantic_gaps` 或
`open_questions`，后续 artifact 生成不应消费未通过 review gate 的 IR。

## 后续演进方向

建议按以下顺序推进：

1. 强化 `representation` typed schema。优先支持 `interface_decl`、
   `combinational_relation`、`sequential_update`、`reset_rule`、`state_machine`、
   `temporal_rule` 和 `protocol_rule`。
2. 增加 signal/port/clock/reset 语义层，让 manifest fields、DesignIR signals 和 spec
   subjects 之间有明确映射。
3. 将 FSM 作为一等结构，显式表示 states、initial/reset state、transitions、outputs 和
   default/illegal behavior。
4. 扩展 source claim extractor，覆盖 Markdown tables、VerilogEval 风格端口描述、
   register maps 和简单 waveform/example。
5. 定义 human-in-loop patch/action 格式，例如 answer question、accept element、
   reject element、split claim、merge claims、add semantic gap。
6. 建立 VerilogEval 小集合 golden SemanticSpecIR benchmark，例如 zero、notgate、hadd、
   dff、fsm2s。
7. 新增 `ArtifactPlan` 层，将 `SemanticSpecIR` 映射为 ref model plan、SVA plan 和
   human review plan。该层可以判断 backend support，但判断结果不应回写为 Spec2IR 语义。

## 设计原则

- `SemanticSpecIR` 是可信语义来源，不是 codegen backend 的能力报告。
- 每条规范性 claim 必须被完整语义、问题或 gap 覆盖。
- 完整性 review 必须从 source spec 重新计算 expected claims。
- LLM 可以抽取和修复结构，但不能绕过 source traceability。
- 对不确定语义保持诚实：记录 gap 或 open question 优于生成看似完整但不可审计的规则。
