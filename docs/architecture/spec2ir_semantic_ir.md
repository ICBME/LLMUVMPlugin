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
Spec2Backend/FeedbackCodegen/
  loop.py                 # feedback-driven LLM artifact generation loop
  ref_model.py            # RefModelPlan -> reference model adapter
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

## SemanticSpecIR v5 数据模型

顶层对象必须是 JSON object，并包含以下关键字段。

### `schema_version`

当前固定为 `5`。schema 升级时必须同时更新 validator、prompt contract、tests 和本文档。

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
- `fingerprint`: 基于 source id、行号范围、归一化 summary 和 quote 生成的 sha256，
  用于跨轮 repair / review 识别同一 source claim。
- `decomposition`: claim 的原子义务拆解。当前格式为：

```json
{
  "version": 1,
  "atomic_obligations": [
    {
      "id": "claim1.obl1",
      "kind": "condition | trigger | response | timing | clock | reset | protocol | interface_port | operation | state_transition | truth_table_row | constraint | behavior | assumption",
      "text": "atomic obligation text",
      "subjects": ["signal_or_state"],
      "required": true,
      "attributes": {}
    }
  ]
}
```

`decomposition` 不是 backend support 判断；它只表达 source claim 内部必须被 IR 覆盖的语义槽位，
用于后续 AST-to-claim alignment review 和 human-in-loop 补全。

source claim extraction 当前按 markdown/source block 解析：

- 普通 paragraph：支持跨行合并，并按句子拆分为 claim。
- bullet / numbered list：每个 list item 作为独立 claim，支持缩进续行。
- markdown 或纯文本 pipe table：header 后每个 data row 生成一个 `truth_table_row`
  claim，例如 `Truth table row: when x=0, y=1`。
- fenced code block 和 heading 不作为 claim。

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
manifest field。`representation.ast` 中的 `field_ref` 节点必须引用已知 manifest
field。

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

`representation` 是 `semantic_elements` 的结构化语义主体。当前实现使用严格
`RepresentationAST v2`，validator 要求它包含：

- `ast_version`: 当前固定为 `2`。
- `kind`: 受控 representation kind。
- `text`: 非空字符串，只作为 human review aid。
- `ast`: 严格 AST node object，是语义真实性来源。

当前允许的 representation kind：

```text
combinational_relation
constraint
example_trace
interface_decl
protocol_rule
reset_rule
sequential_update
state_machine
temporal_rule
textual_formalization
```

当前允许的核心 AST node 包括：

```text
assignment
conditional_assignment
constant_relation
operation_relation
interface_decl
reset_rule
sequential_update
state_transition
fsm
temporal_rule
protocol_rule
constraint
semantic_claim
clock_reset_context
latency_rule
handshake_rule
signal_binding
field_ref
signal_ref
state_ref
literal
unary_op
binary_op
compare
mux
concat
slice
call
text_expr
```

validator 会检查：

- representation 只能包含 `ast_version`、`kind`、`text`、`ast` 和可选 `subjects`。
- `ast.node` 必须是允许的 node。
- root node 必须匹配 `representation.kind`。
- 每类 node 只能包含该 node schema 允许的字段。
- `semantic_claim` 和含有占位信号/条件的 AST 只能配合 blocking `formalization_status` 使用，
  不能被当作完整形式化。
- `temporal_rule` 如果使用 typed latency/delay 语义，必须提供 `clock` 或
  `clock_reset_context.clock` 才能算完整形式化。
- `clock_reset_context` 必须至少包含 clock 或 reset；reset polarity/synchrony 使用受控枚举。
- `handshake_rule` 的 valid 和 ready 必须是 ref node，且不能引用同一个信号。
- `protocol_rule.property` 是必填项；`handshake_rule` 必须通过 `protocol_rule.context.clock`
  给出采样 clock 才能算完整形式化。
- `latency_rule.trigger` / `response` 不能停留在 `text_expr`，否则只能作为 placeholder coverage。
- `field_ref.name` 如果 manifest 已提供，必须是已知 manifest field。
- `signal_ref`、`state_ref` 等 spec/RTL 实体必须有非空名称，但不强制绑定 manifest。

示例：

```json
{
  "ast_version": 2,
  "kind": "combinational_relation",
  "text": "When in=0, out=1.",
  "ast": {
    "node": "conditional_assignment",
    "condition": {
      "node": "compare",
      "op": "eq",
      "left": {"node": "field_ref", "name": "in"},
      "right": {"node": "literal", "value": 0}
    },
    "target": {"node": "signal_ref", "name": "out"},
    "value": {"node": "literal", "value": 1}
  }
}
```

设计上，`representation` 表达“语义是什么”，不表达“哪个 backend 支持它”。如果 claim
无法安全转成严格 AST，应使用 blocking `formalization_status`、`open_questions` 或
`semantic_gaps`，不能退回旧式自由文本结构。
`semantic_claim` 只保留原文语义和溯源，不代表已完成机器可检查的形式化；review 会把它计入
placeholder coverage，并进入 human-in-loop。
`text_expr` 仍可作为 leaf expression 或审查辅助，但 text-only 的 `temporal_rule`、
`protocol_rule`、`constraint` root，以及 latency endpoint 中的 `text_expr` 不算完整形式化，
必须进入 human-in-loop 或继续 repair 成 typed AST。

### RepresentationAST v2 扩展方向

v2 在 v1 的组合逻辑、FSM、temporal/protocol 容器之上，优先补齐后续 refmodel/SVA 所需的
时序上下文和协议语义：

- `clock_reset_context`: 表达 clock edge、reset signal、reset polarity 和 sync/async 属性。
- `latency_rule`: 表达 trigger、response 和 delay range，支撑 SVA 的 `|-> ##[m:n]` 类属性。
- `handshake_rule`: 表达 valid/ready transfer、payload 和可选 latency。
- `signal_binding`: 表达 spec subject 到 manifest field 或 RTL signal 的绑定，为后续 DesignIR
  一致性检查留接口。
- `context` 字段可挂在 `temporal_rule`、`protocol_rule`、`sequential_update`、`reset_rule` 和
  `fsm` 上，避免 clock/reset 信息散落在自然语言 `text_expr` 中。

### AST 检查策略

AST 检查分三层：

- 结构检查：`ast_version`、allowed node、allowed keys、required fields、root node 与
  `representation.kind` 对齐。
- 类型检查：ref node、clock event、delay range、valid/ready、reset polarity/synchrony 等使用
  受控 schema 和枚举。
- 完整性检查：`semantic_claim`、text-only temporal/protocol/constraint root、latency endpoint
  中的 `text_expr`、缺少 clock context 的 temporal/protocol rule，以及占位 target/condition
  只能使用 blocking `formalization_status`。

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

- 由完整 semantic element 覆盖，且没有 unresolved open question / semantic gap obligation：
  可通过 completeness。
- 只被 blocking semantic element、open question 或 semantic gap 覆盖：进入
  `needs_human_input`。
- 完全未覆盖：`failed`。

`completeness` report 包含：

- `normative_claims`: 从 source spec 重新抽取出的 expected normative claim ids。
- `covered_claims`: 已被完整 semantic element 覆盖，且没有 obligation-level 缺口的 claim ids。
- `trace_covered_claims`: 被 semantic element、open question 或 semantic gap 以任意形式引用的
  claim ids，用于审查覆盖链路，但不代表语义完整。
- `partial_claims`: 存在不完整覆盖的 claim ids，包括缺少形式化 obligation 的 semantic element、
  open question 或 semantic gap。
- `placeholder_only_claims`: 只有 incomplete semantic element、open question 或 semantic gap 覆盖的
  claim ids。
- `uncovered_claims`: 没有任何覆盖的 claim ids。
- `claim_obligations`: 每个 claim 的完整性状态和 missing obligations。missing obligation 会记录
  `path`、`code`、`message`，并按来源附带 `semantic_element_id`、`open_question_id` 或
  `semantic_gap_id`，例如 `missing_temporal_clock`、`text_trigger`、`missing_protocol_clock`、
  `blocking_open_question`、`semantic_gap_requires_resolution` 或 `semantic_claim_placeholder`。
- `obligation_coverage`: claim decomposition 到 typed `RepresentationAST` 的覆盖矩阵。每个
  `atomic_obligation` 会被标为 `covered`、`partial`、`uncovered`、`blocked_by_question` 或
  `blocked_by_gap`，并记录覆盖它的 semantic element、open question 或 semantic gap。
- `source_claim_coverage`: source semantic span 到 IR `spec_claims` 的覆盖报告。review 会重新扫描
  source spec 中带有 `must`、`shall`、`when`、`reset`、`valid/ready`、`clock/cycle`、operation、
  interface、state transition、truth table 等语义信号的 fragment，并检查这些 fragment 是否被
  IR `spec_claims` 以相同 source line range 和文本语义覆盖。未覆盖 span 会生成 blocking
  completeness warning，表示当前 IR 无法证明源文本语义已经进入 claim 层。

Completeness obligation 由 AST node checker registry 产生；`semantic_element_has_complete_formalization()`
只是 semantic element structured obligations 的 bool wrapper；open question 和 semantic gap 也会在
claim 级 completeness review 中生成 blocking obligation，避免完整 AST 与未解决语义补充项并存时被误判通过。
此外，`semantic_obligation_coverage.py` 会把 `spec_claims[].decomposition.atomic_obligations[]`
逐条对齐到 typed AST。obligation 到 AST 的匹配通过 `OBLIGATION_AST_MATCHERS` registry
扩展，新增 obligation kind 时应注册对应 matcher，而不是在主流程继续堆分支。例如：

- `operation` 必须由 `operation_relation` 覆盖，并要求 typed operands；只保留 operation name
  而没有 operand 引用时会产生 `operation_operands_missing`。
- `trigger` / `response` 必须由 `latency_rule`、`implication`、event 或 assignment 类节点覆盖，
  不能停留在 `text_expr`。
- `timing` 必须由 `delay_range` 覆盖；clock event 只覆盖 `clock` obligation。
- `protocol` 可由 `protocol_rule.property.handshake_rule` 覆盖。
- `state_transition` 可由 `state_transition` 或 `fsm.transitions` 覆盖。
- `truth_table_row` 可由 typed `conditional_assignment` 或后续 truth-table AST 覆盖，不能由
  无条件 `constant_relation` 覆盖。

这层检查仍然只判断 `SemanticSpecIR` 是否完整表达 source semantics，不判断 ref model、SVA 或
其他 backend 是否支持 lowering。

## Backend readiness

`Spec2Backend/BackendReadiness` 是 `SemanticSpecIR` 和具体 backend lowering 之间的独立分析层。
它不修改 `SemanticSpecIR`，也不生成 ref model 或 SVA，只输出每个 semantic element / claim 的
backend 接入状态。

`analyze_backend_readiness()` 输入：

- `semantic_ir`: 已生成的 `SemanticSpecIR`。
- `review`: 可选的 `review_semantic_spec_ir()` 报告。
- `require_review_passed`: 为 `True` 时，review 未 `passed` 会把整体 readiness 标记为
  `blocked_by_review`。

readiness report 包含：

- `review_gate`: review 是否提供、是否通过、是否阻塞 backend 接入。
- `summary`: semantic element 总数、ref model ready 数、SVA ready 数、需要人工输入数、
  unsupported 数。
- `elements`: 每个 semantic element 的 `representation_kind`、root `ast_node`、
  `support_status`、`recommended_backends`、`lowering_targets`、`blockers` 和 traceability。
- `claims`: claim 到 semantic elements 和推荐 backend 的聚合视图。

当前 readiness 策略：

- `combinational_relation` 的 `assignment`、`constant_relation`、`conditional_assignment` 和已知
  `operation_relation` 可进入 `ref_model`。
- `state_machine`、`sequential_update`、`reset_rule` 中的 typed state/update AST 可进入
  step-based `ref_model`。
- `temporal_rule`、`protocol_rule`、`constraint` 可进入 `sva`；需要 clock 的规则必须具备显式
  `clock_event` / `clock_reset_context`。
- `semantic_claim`、`text_expr`、blocking `formalization_status` 或 representation completeness
  issue 会标为 `needs_human_input`。
- interface declaration 目前视为 backend metadata；example trace 需要后续 test-vector backend。

旧 `rtlagent-codegen` CLI 已随 `rtlagent_bfm.codegen` 移除。当前入口是
`Spec2Backend.BackendReadiness.analyze_backend_readiness()`，调用方负责读写 JSON artifact。

## RefModelPlan

`Spec2Backend/RefModelPlan` 是 `SemanticSpecIR` 到 ref model 后端之间的计划层。它只消费
`ref_model` backend ready 的 semantic elements，不处理 SVA-only elements，也不直接生成
Python 插件或 RefModelIR。

`build_ref_model_plan()` 输入：

- `semantic_ir`: 已生成的 `SemanticSpecIR`。
- `readiness`: 可选的 `analyze_backend_readiness()` 报告；未提供时会自动生成。
- `require_readiness_ready`: 为 `True` 时，readiness 非 `ready` / `partial` 会返回
  `blocked_by_readiness`。

plan report 包含：

- `rules`: 可进入 ref model codegen 的结构化规则，保留 `semantic_element_id`、`claim_ids`、
  `evidence` 和 `source_ast_node`。
- `blocked_items`: 目标是 ref model 但尚不能规划的 semantic elements，例如 textual fallback、
  blocking formalization status、unsupported ref model operation。
- `not_applicable`: SVA-only、metadata 或 test-vector backend 的 semantic elements。
- `summary`: rule、blocked、not applicable 数量。

当前 rule 类型：

- `operation_relation`: 已知 operation，例如 `sha256` 或 `not_gate`，以及 typed operands。
- `assignment` / `constant_relation` / `conditional_assignment`: 组合逻辑规则。
- `reset_rule` / `sequential_update` / `state_machine` / `state_transition`: 简单状态更新计划。

RefModelPlan expression 使用稳定 JSON 表达，例如 `field`、`signal`、`literal`、`compare`、
`unary_op`、`binary_op`、`mux`、`concat`、`slice`、`cast`、`clock_event` 和 `state_transition`。
后续 ref model backend 应只消费 RefModelPlan，而不是直接消费 `RepresentationAST`。

旧 `rtlagent-codegen build-ref-model-plan` CLI 已移除。当前入口是
`Spec2Backend.RefModelPlan.build_ref_model_plan()`。

推荐的可信生成路径是将 plan 交给
`Spec2Backend.RefModelDSL.generate_ref_model_ir_with_feedback()`：

- LLM 生成 `RefModelIR` JSON。
- `Spec2Backend/RefModelDSL` 执行 schema/type/extern/Z3 verification。
- 框架确定性生成 UVM wrapper，并通过现有 `GeneratedPluginBundle` / manifest overlay 接入。

`Spec2Backend.FeedbackCodegen.generate_ref_model_with_feedback()` 仍可作为 legacy Python
file-bundle fallback，但它不提供 IR-level formal proof。

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

## API Usage

旧 `rtlagent_bfm.codegen.cli` 已移除；当前默认只承诺 Python API。调用方直接使用
`Spec2Backend.Spec2IR`、`Spec2Backend.BackendReadiness`、`Spec2Backend.RefModelPlan` 和
`Spec2Backend.FeedbackCodegen` 的函数，并自行管理 JSON artifact 路径。

```python
from LLMPlugin import create_backend
from Spec2Backend.BackendReadiness import analyze_backend_readiness
from Spec2Backend.RefModelPlan import build_ref_model_plan
from Spec2Backend.FeedbackCodegen import generate_ref_model_with_feedback

readiness = analyze_backend_readiness(semantic_ir, review=review, require_review_passed=True)
plan = build_ref_model_plan(semantic_ir, readiness=readiness)
backend = create_backend("langgraph", model="...")
result = generate_ref_model_with_feedback(
    plan,
    manifest_path="targets/demo.toml",
    spec_paths=("specs/demo.md",),
    output_dir="generated/ref_model_codegen/demo",
    llm_backend=backend,
    golden_cases=(),
)
```

## 当前测试覆盖

核心测试分布在：

```text
tests/test_feedback_codegen.py
tests/test_ref_model_codegen_semantic_ir.py
tests/test_generated_plugin_integration.py
```

覆盖能力包括：

- rule-based SemanticSpecIR 生成。
- LLM response normalization。
- LLMPlugin registry、Callable backend 和 LangGraph delegate。
- FeedbackCodegen strict bundle validation、static validation、subprocess contract/golden
  evaluation 和 retry feedback。
- source quote/hash traceability。
- completeness review 重新从 source spec 抽取 claims。
- blocking formalization status 进入 `needs_human_input`。
- `representation` 必须是 strict `RepresentationAST v2`。
- legacy `representation.type` / `representation.fields[]` 会被拒绝。
- `field_ref` AST 节点必须引用 manifest fields。
- repair loop 成功、backend error 和无 LLM 情况。

推荐回归命令：

```sh
uv run python -m pytest \
  tests/test_feedback_codegen.py \
  tests/test_ref_model_codegen_semantic_ir.py \
  tests/test_generated_plugin_integration.py \
  -q
uv run python -m py_compile \
  Spec2Backend/FeedbackCodegen/*.py \
  Spec2Backend/Spec2IR/*.py \
  Spec2Backend/__init__.py
git diff --check
```

## 当前能力边界

当前 `SemanticSpecIR` 已经可以作为可信审查链路的基础，但还不能声称“完全且可靠地抽取
任意 spec 的所有语义”。主要限制：

- rule-based extractor 只能生成保守 draft，真正的语义抽取依赖 LLM 或人工补全。
- `RepresentationAST v2` 已经强制 node kind、root node、allowed keys 和 field_ref 引用，
  但表达范围仍是第一版，复杂协议/波形/寄存器表还需要继续扩展 node schema。
- claim extraction 对 Markdown prose 有基本支持，但尚未覆盖表格、时序图、波形图、伪代码、
  register map、协议时序表等复杂 source 格式。
- `confidence` 当前只做范围校验，还没有基于证据强度或多轮一致性的评分策略。
- human answers 当前只作为 review gate 信号，尚未定义标准 patch/action 格式。
- optional DesignIR 当前只做可加载性检查，尚未参与 signal/port/clock/reset 层的一致性验证。

这些限制不会破坏现有审查链路，因为无法完整形式化的语义必须进入 blocking
`formalization_status`、`semantic_gaps` 或 `open_questions`，后续 artifact 生成不应消费未通过
review gate 的 IR。

## 后续演进方向

建议按以下顺序推进：

1. 扩展 `RepresentationAST` typed schema。继续强化 `temporal_rule`、`protocol_rule`、
   `example_trace` 和 register-map/source-table 表达。
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
