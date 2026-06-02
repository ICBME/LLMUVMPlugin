# Coverage Feedback Design

本文档描述 coverage feedback 的结构化数据层设计。目标是把工具原始覆盖率
artifact 先归一化为稳定的 coverage export，再派生出可供 heuristic advisor 和
LLM advisor 共同消费的 `rtl_gap`。

当前实现优先覆盖 RTL structural coverage；functional coverage 后续按同一接口扩展。

## 设计目标

- 将 Verilator `.dat` / LCOV `.info` 等工具格式与 advisor 策略解耦。
- 为每个 coverage point 和 RTL gap 提供稳定 `id`，支持跨轮比较和消除已解决 gap。
- 让无 LLM 链路和 LLM 链路消费同一份结构化 `rtl_gap`，避免分叉解析。
- 保留源码上下文和 evidence，既能被规则匹配，也能压缩后放进 LLM prompt。
- 保持旧 coverage summary 字段兼容，避免破坏现有评估脚本。

## 分层数据流

```text
raw coverage artifacts
  .info / .dat / future functional json
        |
        v
coverage_export
  normalized points + totals + stable point ids
        |
        v
rtl_gap_summary
  grouped uncovered RTL gaps + evidence + advisor hints
        |
        +--> heuristic advisor
        |
        +--> LLM prompt/advisor
        |
        v
mutation directives
        |
        v
next corpus generation
```

实际 fuzz 闭环按执行频率拆成三层反馈：

```text
Layer 1: coverage -> rtl_gap -> mutation plan
  低频，重分析，可使用 LLM 处理复杂 gap

Layer 2: mutation rounds -> per-gap feedback
  中频，比较多轮 gap 状态，判断 gap resolved/improved/stale

Layer 3: mutation direction feedback
  高频，纯规则统计，不使用 LLM，快速调整 mutation direction 权重
```

Layer 1 负责理解当前覆盖率缺口，Layer 2 负责判断 gap 是否被解决，Layer 3
负责判断当前 mutation 方向是否值得继续。三层共享 summary/directives/gap id，但
不在高频路径上重复做源码分析或 LLM 推理。

## 模块职责

`py/fuzz_feedback/coverage_export.py`

- 定义通用 `CoveragePoint` 和 `CoverageExport`。
- 负责 point-level 稳定 `id`、hit/uncovered 判断、按 kind/file/module 汇总。
- 不理解 Verilator、LCOV 或 DUT 语义。
- 后续 functional coverage point 也应复用该数据模型。

`py/fuzz_feedback/rtl_structure_coverage.py`

- 解析 LCOV line coverage 和 Verilator coverage `.dat`。
- 将原始记录归一化为 `CoveragePoint(domain="rtl_structure")`。
- 暴露 `build_rtl_structure_coverage_export()` 作为结构化导出入口。
- 暴露 `build_rtl_structure_coverage()` 作为兼容旧 summary 的入口。

`py/fuzz_feedback/rtl_gap.py`

- 从 uncovered structural `CoveragePoint` 构建 `rtl_gap_summary`。
- 按 `file + line` 聚合同一源码行上的 line/branch/expression/toggle 点。
- 对无 line 的点，按 `file + module + kind + object/name` 聚合。
- 为每个 gap 生成稳定 `id`、源码上下文、evidence 和 advisor hints。
- 负责 gap 排序，不负责生成 mutation directives。

`py/fuzz_feedback/coverage.py`

- 汇总 coverage export、`rtl_gap_summary`、functional coverage 和 stimulus summary。
- 顶层 summary 同时保留 `rtl_structure_coverage` 和 `rtl_gap_summary`，便于 advisor 直接读取。

`py/fuzz_feedback/advisors.py`

- 消费结构化 summary，尤其是 `rtl_gap_summary.top_gaps`。
- 不应解析 `.info` / `.dat`，也不应依赖 Verilator metadata 的细节。

`py/fuzz_feedback/mutation_planner.py`

- 将清晰 `rtl_gap` 转换为无 LLM mutation directives。
- 将无法确定字段映射的复杂 `rtl_gap` 压缩为 LLM prompt 输入。
- 当前只实现保守规则，不做复杂 RTL 静态分析。

`py/fuzz_feedback/feedback_loop.py`

- 实现中频 Layer 2 per-gap feedback 和高频 Layer 3 mutation direction feedback。
- Layer 2 消费上一轮 summary、当前 summary、当前 directives、可选上一轮 gap feedback
  和可选 Layer 3 mutation feedback，输出 gap 状态和 next action。
- Layer 3 消费上一轮 summary、当前 summary、当前 directives 和可选上一轮 feedback，
  根据新增 case、replay 成功情况、结构覆盖 delta、functional bin delta 等低成本信号评分。
- 输出每个 direction 的 `score`、`decision`、`updated_weight` 和 `stale_count`。
- 不调用 LLM，不解析原始 `.info` / `.dat`，不做 RTL 源码语义分析。

`scripts/layer2_gap_feedback_eval.py`

- 离线评估 Layer 2 per-gap 反馈效果。
- 输入两份 coverage summary、本轮使用的 directives，以及可选上一轮 gap feedback /
  Layer 3 mutation feedback。
- 输出 gap feedback JSON 和 Markdown 报告。

`scripts/layer3_mutation_feedback_eval.py`

- 离线评估 Layer 3 反馈效果。
- 输入两份 coverage summary 和本轮使用的 directives。
- 输出 mutation feedback JSON、更新后的 directives JSON 和 Markdown 报告。

## Coverage Export Schema

`CoverageExport.to_json()` 输出版本化结构：

```json
{
  "schema_version": 1,
  "domain": "rtl_structure",
  "target": "secworks_sha256",
  "sources": {
    "lcov_info": ".../secworks_sha256_coverage.info",
    "verilator_dat": ".../secworks_sha256_coverage.dat"
  },
  "point_count": 100,
  "uncovered_point_count": 12,
  "totals": {
    "total": 100,
    "hit": 88,
    "uncovered": 12,
    "coverage": 0.88
  },
  "by_kind": {},
  "by_file": {},
  "by_module": {},
  "uncovered_points": []
}
```

`CoveragePoint` 的 JSON 形式：

```json
{
  "id": "stable-point-id",
  "domain": "rtl_structure",
  "kind": "branch",
  "file": ".../sha256_core.v",
  "line": 123,
  "count": 0,
  "hit": false,
  "module": "sha256_core",
  "object": "state == CTRL",
  "source": "verilator_dat",
  "name": null,
  "metadata": {}
}
```

字段说明：

- `id`：由 domain/source/kind/file/line/module/object/name 生成的稳定短 hash。
- `domain`：当前为 `rtl_structure`；后续可加入 `uvm_functional`。
- `kind`：结构覆盖类型，例如 `line`、`branch`、`expression`、`toggle`。
- `source`：原始来源，例如 `lcov_info` 或 `verilator_dat`。
- `metadata`：预留给后续工具特有字段，advisor 默认不依赖它。

## RTL Gap Schema

`rtl_gap_summary` 是 advisor 的主要输入：

```json
{
  "schema_version": 1,
  "domain": "rtl_gap",
  "target": "secworks_sha256",
  "total": 8,
  "total_points": 14,
  "priority_order": ["branch", "expression", "line", "fsm", "user", "toggle"],
  "by_kind": {
    "branch": 4,
    "line": 3,
    "toggle": 7
  },
  "by_file": {},
  "by_module": {},
  "top_gaps": []
}
```

单个 gap：

```json
{
  "schema_version": 1,
  "domain": "rtl_gap",
  "id": "stable-gap-id",
  "primary_kind": "branch",
  "file": ".../sha256_core.v",
  "line": 123,
  "module": "sha256_core",
  "code": "if (next) begin",
  "context": [
    {"line": 121, "code": "..."},
    {"line": 122, "code": "..."},
    {"line": 123, "code": "if (next) begin"}
  ],
  "kinds": {
    "branch": 1,
    "line": 1
  },
  "point_count": 2,
  "objects": ["next"],
  "object_count": 1,
  "priority": 100,
  "actionability": "unknown",
  "evidence": {
    "point_ids": ["stable-point-id-a", "stable-point-id-b"],
    "kinds": {"branch": 1, "line": 1},
    "objects": ["next"],
    "sources": ["lcov_info", "verilator_dat"]
  },
  "advisor_hints": [
    {"type": "source_keyword", "value": "next"},
    {"type": "signal_name", "value": "next"}
  ],
  "source": "rtl_structure"
}
```

`primary_kind` 由 gap 内最高优先级 uncovered point 决定。当前优先级为：

```text
branch > expression > line > fsm > user > toggle
```

该顺序刻意降低 toggle 噪声，优先把控制流和表达式 gap 暴露给 advisor。

## Advisor Hints

`advisor_hints` 是无 LLM 链路的规则入口，也是 LLM prompt 的紧凑语义提示。
当前实现从源码行、上下文、module 和 Verilator object 中提取关键词和信号名。

示例 hint：

```json
[
  {"type": "source_keyword", "value": "address"},
  {"type": "source_keyword", "value": "state"},
  {"type": "signal_name", "value": "message_len"}
]
```

无 LLM advisor 可基于 hint 生成保守 directives：

- `len` / `length` / `size` / `pad` / `block`：尝试 hex 长度 bucket。
- `address` / `addr` / `case`：尝试 MMIO 地址或读写组合。
- `mode` / `op` / `key`：尝试 manifest enum/int field cross。
- `reset` / `error` / `default`：降权，或等待 waiver/unreachable 标注。

## Heuristic And LLM Paths

两条链路都应消费同一个 `rtl_gap_summary`：

```text
summary["rtl_gap_summary"]["top_gaps"]
```

无 LLM 链路：

- 使用 `primary_kind`、`advisor_hints`、`module`、`code` 和 manifest schema 做规则匹配。
- 输出 schema-compatible mutation directives。
- 不应读取原始 `.dat` 或 `.info`。
- 当前实现只处理清晰 gap：字段名直接命中、长度/多块关键词命中单个 variable
  hex 字段、以及地址/读写关键词命中 manifest 中已有字段。
- 无法清晰映射的 gap 会进入 `complex_gaps`，不由 heuristic 猜测。

LLM 链路：

- Prompt 中传 `complex_gaps`、manifest schema、stimulus summary 和 heuristic baseline。
- 每个 gap 应包含 `code/context/evidence/advisor_hints`，避免把大量原始 toggle point 塞入 prompt。
- LLM 输出仍必须通过 directive validation 和 corpus validation。

当前 `propose_directives(summary)` 的组合顺序为：

```text
functional gap directives
+ clear rtl_gap heuristic directives
+ sparse/schema refresh fallback
```

如果存在 `complex_gaps`，`build_llm_prompt()` 会额外加入
`rtl_gap_mutation_prompt`，供 LLM 生成补充 directives。

## Layer 2 Per-Gap Feedback

Layer 2 是中频反馈层，目标是跨 mutation round 跟踪每个 `rtl_gap` 的解决进度。
它不重新生成 coverage gap，也不直接调用 LLM；它只判断：

```text
这个 gap 是新出现、仍 open、已 improved、已 resolved，还是多轮 stale？
下一步应该继续当前方向、换策略、升级给 LLM，还是降权？
```

### 输入

`build_gap_feedback(previous_summary, current_summary, directives, previous_gap_feedback=None, mutation_feedback=None)`
接收：

- `previous_summary`：上一轮 coverage feedback summary。
- `current_summary`：当前轮 coverage feedback summary。
- `directives`：本轮用于生成 corpus 的 mutation directives。
- `previous_gap_feedback`：可选，上一轮 Layer 2 状态，用于累计 attempts/stale。
- `mutation_feedback`：可选，Layer 3 direction feedback，用于感知 direction 是否被抑制。

Layer 2 使用的关键字段：

- `rtl_gap_summary.top_gaps[*].id`
- `rtl_gap_summary.top_gaps[*].primary_kind`
- `rtl_gap_summary.top_gaps[*].evidence.point_ids`
- `rtl_structure_coverage.coverage_export.uncovered_points[*].id`
- `directives[*].gap_ids`
- `mutation_feedback.directions[*].decision`

point 级计数优先使用 `coverage_export.uncovered_points`。如果 summary 是旧格式，
缺少 coverage export，Layer 2 会退回使用 gap evidence point ids，但此时只能判断
gap 是否仍在 `top_gaps` 中，无法精确判断 point count 是否下降。

### Status

Layer 2 输出的 gap status：

```text
new          当前 summary 新出现的 gap
open         gap 仍存在，本轮没有明确尝试
improved     gap 仍存在，但 uncovered point count 降低
resolved     gap 从当前 summary 消失
stale        gap 被尝试后仍无改善
regressed    之前 resolved 的 gap 重新出现
```

状态判定规则：

```text
previous gap 存在，current gap 不存在
  -> resolved

current gap 存在，previous gap 不存在
  -> new

current uncovered point count < previous uncovered point count
  -> improved

current gap 存在，且本轮 directive 通过 gap_ids 尝试过该 gap，但 point count 无下降
  -> stale

current gap 存在，但本轮没有 gap-specific directive 尝试
  -> open

previous status 为 resolved，current gap 又出现
  -> regressed
```

### Next Action

Layer 2 将 status 和 gap 类型转换成 next action：

```text
done              resolved gap，不再继续尝试
continue          improved gap 或当前尝试仍可继续
plan              new/open gap，需要 Layer 1 或 planner 生成策略
retry             regressed gap，重新纳入计划
try_alternative   关联 direction 被 Layer 3 抑制，或多轮 stale 后需要换策略
escalate_to_llm   branch/expression/fsm 等复杂 gap 多轮 stale
deprioritize      toggle/user 等低价值 gap 多轮 stale
```

当前阈值为：

```text
GAP_STALE_ESCALATE_THRESHOLD = 2
```

也就是说，复杂 gap 连续两轮 stale 后会进入 `escalate_to_llm`。这不会立刻调用
LLM，而是为 Layer 1 下一次低频分析提供明确候选。

### Output Schema

`build_gap_feedback()` 输出：

```json
{
  "schema_version": 1,
  "layer": "per_gap_feedback",
  "target": "secworks_sha256",
  "total_gaps": 4,
  "status_counts": {
    "improved": 1,
    "new": 1,
    "resolved": 1,
    "stale": 1
  },
  "next_action_counts": {
    "continue": 1,
    "done": 1,
    "escalate_to_llm": 1,
    "plan": 1
  },
  "gaps": {
    "gap-id": {
      "id": "gap-id",
      "status": "stale",
      "next_action": "escalate_to_llm",
      "primary_kind": "branch",
      "priority": 100,
      "file": ".../sha256_core.v",
      "line": 123,
      "module": "sha256_core",
      "previous_point_count": 1,
      "current_point_count": 1,
      "attempt_count": 2,
      "stale_count": 2,
      "success_count": 0,
      "attempted_directives": ["try_old", "try_current"],
      "current_attempted_directives": ["try_current"],
      "direction_decisions": {
        "try_current": "decrease_weight"
      },
      "evidence": {}
    }
  }
}
```

### Evaluation Script

Layer 2 可用两份 summary 离线评估：

```bash
uv run python libafl_bfm_fuzz/scripts/layer2_gap_feedback_eval.py \
  --previous-summary previous_coverage_summary.json \
  --current-summary current_coverage_summary.json \
  --directives applied_mutation_directives.json \
  --previous-gap-feedback previous_layer2_gap_feedback.json \
  --mutation-feedback layer3_mutation_feedback.json \
  --feedback-out layer2_gap_feedback.json \
  --markdown-out layer2_gap_feedback.md
```

也支持 run-dir 形式：

```bash
uv run python libafl_bfm_fuzz/scripts/layer2_gap_feedback_eval.py \
  --target secworks_sha256 \
  --previous-run-dir libafl_bfm_fuzz/coverage/feedback_compare/secworks_sha256/baseline \
  --current-run-dir libafl_bfm_fuzz/coverage/feedback_compare/secworks_sha256/heuristic \
  --directives libafl_bfm_fuzz/coverage/feedback_compare/secworks_sha256/baseline/secworks_sha256_mutation_directives.json \
  --out-dir libafl_bfm_fuzz/coverage/feedback_compare/secworks_sha256/layer2_heuristic
```

脚本输出：

- `*_layer2_gap_feedback.json`：per-gap 状态和 next action。
- `*_layer2_gap_feedback.md`：人读表格，按需优先展示 `escalate_to_llm`、
  `try_alternative`、`retry` 等高价值 action。

## Layer 3 Mutation Feedback

Layer 3 是最高频反馈层，目标是用便宜的统计信号快速判断 mutation direction
是否有效，并更新下一轮 directives。它不解决“为什么这个 RTL gap 没覆盖”的语义问题；
这个问题留给 Layer 1/Layer 2。Layer 3 只回答：

```text
这个 mutation direction 最近是否带来了覆盖收益？
是否应该增加权重、降低权重，还是临时抑制？
```

### 输入

`build_mutation_feedback(previous_summary, current_summary, directives, previous_feedback=None)`
接收：

- `previous_summary`：上一轮 coverage feedback summary。
- `current_summary`：当前轮 coverage feedback summary。
- `directives`：上一轮用于生成 corpus 的 mutation directives。
- `previous_feedback`：可选，上一轮 Layer 3 feedback，用于累计 `stale_count`。

它依赖的 summary 字段都是已有轻量统计：

- `stimulus_summary.origin_counts`：每个 directive origin 生成了多少 corpus case。
- `uvm_functional_coverage.origin_counts`：每个 directive origin replay 成功了多少 case。
- `uvm_functional_coverage.bins/crosses`：新增 functional bins。
- `rtl_structure_coverage.totals.coverage`：整体结构覆盖率变化。
- `rtl_structure_coverage.coverage_export.uncovered_points`：可选，用于 point 级 resolved 统计。
- `rtl_gap_summary.top_gaps`：可选，用于 gap 级 resolved 统计。

如果 summary 是旧格式，缺少 `coverage_export.uncovered_points` 或 `rtl_gap_summary.top_gaps`，
Layer 3 仍可基于整体 coverage delta、functional bins 和 origin counts 工作，只是
`resolved_point_count` / `resolved_gap_count` 会退化为 0。

### Attribution

当前 corpus/replay summary 是按整体 corpus 聚合的，不是按每个 directive 单独覆盖统计。
因此 Layer 3 采用保守归因：

- `direct`：只有一个 direction 在本轮产生新增 case，收益直接归给它。
- `mixed_proportional`：多个 direction 同时产生新增 case，按新增 case 数比例分配收益。

`mixed_proportional` 不用于精确解释单个 case 的贡献，只用于高频权重调整。精确 gap 级归因
后续由 Layer 2 引入 per-gap 状态和更细的 replay/corpus 记录。

### Scoring

单个 direction 的得分由低成本收益和失败信号组成：

```text
gain =
  structural_resolved_points * 2
+ resolved_gap_count * 3
+ functional_new_bins
+ max(0, structural_coverage_delta) * 10

score = gain / sqrt(max(1, new_generated_cases)) - replay_drop * 0.25
```

其中：

- `new_generated_cases` 来自 `stimulus_summary.origin_counts` 的差值。
- `replay_drop = new_generated_cases - new_replayed_cases`，用于惩罚生成后 replay 失败或未被采样的 case。
- `functional_new_bins` 来自 `bins/crosses` 的新增 hit。
- `structural_resolved_points` 和 `resolved_gap_count` 需要新格式 summary。

### Decision

Layer 3 输出以下 decision：

```text
inactive              本轮没有新增 case，不调整
increase_weight       有正收益，提高权重
decrease_weight       有尝试但没有收益，降低权重
suppress_temporarily  连续 stale 达到阈值，临时禁用该 direction
```

权重更新规则：

```text
increase_weight       min(4.0, max(weight + 0.25, weight * 1.5))
decrease_weight       max(0.1, weight * 0.7)
suppress_temporarily  weight = 0.1, enabled = false
inactive              保持原权重
```

Rust corpus generator 已支持跳过 `enabled=false` 的 directive，因此 Layer 3 的抑制结果
会真实影响下一轮 corpus generation。

### Output Schema

`build_mutation_feedback()` 输出：

```json
{
  "schema_version": 1,
  "layer": "mutation_feedback",
  "target": "secworks_sha256",
  "attribution": "direct",
  "aggregate_delta": {
    "structural_resolved_point_count": 0,
    "resolved_gap_count": 0,
    "functional_new_bin_count": 2,
    "structural_coverage_delta": 0.000482,
    "uncovered_line_delta": 0,
    "total_new_generated_cases": 6,
    "resolved_point_ids": [],
    "resolved_gap_ids": [],
    "new_functional_bins": ["bins:message_length:32..55"]
  },
  "directions": {
    "schema_refresh": {
      "name": "schema_refresh",
      "gap_ids": [],
      "new_generated_cases": 6,
      "new_replayed_cases": 6,
      "replay_drop": 0,
      "attribution": "direct",
      "attribution_share": 1.0,
      "structural_resolved_points": 0,
      "resolved_gap_count": 0,
      "functional_new_bins": 2,
      "score": 0.818464,
      "decision": "increase_weight",
      "previous_weight": 1.0,
      "updated_weight": 1.5,
      "stale_count": 0,
      "success_count": 1
    }
  }
}
```

`update_mutation_directions()` 会把 feedback 写回 directives：

```json
{
  "directives": [
    {
      "name": "schema_refresh",
      "weight": 1.5,
      "feedback_decision": "increase_weight",
      "feedback_score": 0.818464
    }
  ],
  "mutation_feedback": {
    "schema_version": 1,
    "layer": "mutation_feedback",
    "aggregate_delta": {}
  }
}
```

若 decision 为 `suppress_temporarily`，directive 会被标记：

```json
{
  "name": "stale_direction",
  "weight": 0.1,
  "enabled": false,
  "feedback_decision": "suppress_temporarily"
}
```

### Evaluation Script

Layer 3 可用现有 coverage artifacts 离线验证：

```bash
uv run python libafl_bfm_fuzz/scripts/layer3_mutation_feedback_eval.py \
  --target secworks_sha256 \
  --previous-run-dir libafl_bfm_fuzz/coverage/feedback_compare/secworks_sha256/baseline \
  --current-run-dir libafl_bfm_fuzz/coverage/feedback_compare/secworks_sha256/heuristic \
  --directives libafl_bfm_fuzz/coverage/feedback_compare/secworks_sha256/baseline/secworks_sha256_mutation_directives.json \
  --out-dir libafl_bfm_fuzz/coverage/feedback_compare/secworks_sha256/layer3_heuristic
```

也可以直接传 summary 路径：

```bash
uv run python libafl_bfm_fuzz/scripts/layer3_mutation_feedback_eval.py \
  --previous-summary previous_coverage_summary.json \
  --current-summary current_coverage_summary.json \
  --directives applied_mutation_directives.json \
  --feedback-out layer3_mutation_feedback.json \
  --updated-directives-out layer3_updated_directives.json \
  --markdown-out layer3_mutation_feedback.md
```

脚本输出：

- `*_layer3_mutation_feedback.json`：Layer 3 评分结果。
- `*_layer3_updated_directives.json`：可直接用于下一轮 corpus generation 的 directives。
- `*_layer3_mutation_feedback.md`：人读评估报告。

当前在已有 `feedback_compare` artifacts 上的观察：

- `secworks_sha256` heuristic direction `schema_refresh`：functional new bins 增加，decision 为 `increase_weight`。
- `secworks_aes` heuristic direction `schema_refresh`：结构覆盖率明显提升，decision 为 `increase_weight`。
- LLM 多 direction 场景采用 `mixed_proportional` 归因，所有带来收益的 direction 都会增权。

## Backward Compatibility

`build_rtl_structure_coverage()` 继续输出旧字段：

- `totals`
- `by_kind`
- `by_file`
- `by_module`
- `uncovered_points`
- `available_kinds`
- `supported_kinds`

新增字段：

- `coverage_export`
- `rtl_gap_summary`

`coverage.py` 同时将 `rtl_gap_summary` 提升到顶层 summary，方便 advisor 消费：

```json
{
  "rtl_structure_coverage": {
    "coverage_export": {},
    "rtl_gap_summary": {}
  },
  "rtl_gap_summary": {}
}
```

## Functional Coverage Extension

后续 functional coverage 不应直接塞进 `rtl_gap`。建议先导出为同一类
`CoverageExport(domain="uvm_functional")`，例如：

```json
{
  "domain": "uvm_functional",
  "kind": "coverpoint_bin",
  "name": "message_length",
  "object": "56+",
  "count": 0,
  "hit": false
}
```

然后生成独立的 functional gap summary，最后在 advisor 层与 `rtl_gap_summary`
合并推理。例如：

```text
rtl_gap: sha256 next-block branch uncovered
functional_gap: message_length 56+ uncovered
=> generate explicit 56/64-byte message cases
```

这样结构覆盖和功能覆盖保持同一数据模型，但不会混淆 RTL 源码 gap 与目标语义 gap。

## Known Limitations

- 当前 `actionability` 默认为 `unknown`，还没有 waiver/unreachable 机制。
- `advisor_hints` 是轻量关键词抽取，不替代目标专用 coverage advisor plugin。
- `CoverageExport` 默认不导出全量 `points`，避免 summary 过大；调试时可显式请求。
- Layer 2 依赖 `rtl_gap_summary.top_gaps`，如果 gap 被截断出 top list，可能无法稳定跟踪；
  后续应导出 full gap catalog 或 all gap ids。
- Layer 3 使用 aggregate summary 做 direction 归因，多个 direction 混跑时只能按 case 数比例分配收益。
- 如果 summary 缺少 `rtl_gap_summary.top_gaps` 或 `coverage_export.uncovered_points`，
  Layer 2/Layer 3 无法计算 point/gap 级 resolved，只能使用整体 coverage delta 和 functional bins。
- 结构覆盖仍缺少 per-case attribution，无法判断某个 corpus case 是否贡献了某个新 gap。

## Next Steps

1. 将 Layer 2/Layer 3 feedback state 接入 `coverage_feedback.py` CLI，自动读取上一轮 summary/feedback 并写出状态。
2. 让 `mutation_planner.py` 消费 Layer 2 next action：跳过 resolved、升级 stale complex gap、降低低价值 gap。
3. 扩展 Rust generator 支持 variable-length hex directives，例如 `message_lengths`。
4. 为 LLM 输出增加更严格的 directive/case validation。
5. 增加 waiver/unreachable 标注，避免 defensive/default branch 反复污染反馈。
6. 比较 heuristic structural directives 和 LLM structural directives 的覆盖率收益。
7. 按同一 `CoverageExport` 模型接入 functional coverage export。
