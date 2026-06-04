# Coverage Feedback Evaluation

本文档记录 coverage feedback 的离线和端到端评估。最新结果优先放在前面；
2026-06-01 的 functional coverage feedback smoke 评估保留为历史基线。

## 2026-06-04 端到端反馈链路评估

当前已有两组两轮反馈链路 artifacts：

- `libafl_bfm_fuzz/coverage/feedback_chain_compare_env/`：同时使用 RTL code coverage
  和 UVM functional coverage gap 生成反馈。
- `libafl_bfm_fuzz/coverage/feedback_chain_code_only_env/`：反馈生成时忽略 UVM
  functional coverage，只使用 RTL code coverage。

两组实验均比较三种模式：

- `no_feedback`：不使用反馈生成下一轮 corpus。
- `heuristic_feedback`：使用规则型 feedback/directives。
- `llm_feedback`：调用真实 LLM 生成反馈 directives；报告中的 `LLM Real=True` 表示
  本轮确实调用了模型。

### RTL + Functional Feedback

主要报告：

```text
libafl_bfm_fuzz/coverage/feedback_chain_compare_env/feedback_chain_comparison.md
libafl_bfm_fuzz/coverage/feedback_chain_compare_env/feedback_chain_code_coverage.md
```

最终覆盖率：

| Target | Mode | Final Overall | Cases | Uncovered Lines | Functional Gap | LLM Real |
| --- | --- | ---: | ---: | ---: | --- | --- |
| secworks_aes | no_feedback | 95.818% | 13 | 20 | none | False |
| secworks_aes | heuristic_feedback | 95.818% | 13 | 20 | none | False |
| secworks_aes | llm_feedback | 99.633% | 47 | 18 | none | True |
| secworks_sha256 | no_feedback | 98.795% | 6 | 34 | `message_length`: `1..15`, `32..55`, `56+` | False |
| secworks_sha256 | heuristic_feedback | 99.293% | 14 | 14 | none | False |
| secworks_sha256 | llm_feedback | 99.293% | 27 | 14 | none | True |

RTL code coverage 增益：

| Target | Mode | Overall Δ vs No Feedback | Line Δ | Toggle Δ | Branch Δ | Expr Δ |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| secworks_aes | heuristic_feedback | +0.000% | +0.000% | +0.000% | +0.000% | +0.000% |
| secworks_aes | llm_feedback | +3.815% | +0.204% | +4.195% | +0.746% | +0.000% |
| secworks_sha256 | heuristic_feedback | +0.498% | +3.425% | +0.162% | +3.030% | +0.000% |
| secworks_sha256 | llm_feedback | +0.498% | +3.425% | +0.162% | +3.030% | +0.000% |

观察：

- AES 的 heuristic feedback 没有带来增益；真实 LLM feedback 明显提高 toggle 和
  overall coverage，并减少 2 条 uncovered lines。
- SHA-256 的 heuristic feedback 已经能利用 functional `message_length` gap 生成有效
  case，消除 functional gap，并减少 20 条 uncovered lines。
- SHA-256 的 LLM feedback 生成更多 case，但最终 RTL coverage 与 heuristic 相同；
  当前 LLM 的额外价值主要体现在 case 多样性，而非最终覆盖率。
- Layer 2 能观察到 gap resolved/stale/open 状态；Layer 3 在有效方向上产生
  `increase_weight`，说明三层反馈状态已经能贯穿评估链路。

### RTL Code-Only Feedback

主要报告：

```text
libafl_bfm_fuzz/coverage/feedback_chain_code_only_env/feedback_chain_code_only_comparison.md
libafl_bfm_fuzz/coverage/feedback_chain_code_only_env/feedback_chain_code_only_coverage.md
```

最终覆盖率：

| Target | Mode | Final Overall | Cases | Uncovered Lines | LLM Real |
| --- | --- | ---: | ---: | ---: | --- |
| secworks_aes | no_feedback | 95.818% | 13 | 20 | False |
| secworks_aes | heuristic_feedback | 95.818% | 13 | 20 | False |
| secworks_aes | llm_feedback | 99.701% | 91 | 18 | True |
| secworks_sha256 | no_feedback | 98.795% | 6 | 34 | False |
| secworks_sha256 | heuristic_feedback | 99.293% | 14 | 14 | False |
| secworks_sha256 | llm_feedback | 99.293% | 24 | 14 | True |

与 RTL + functional 版本相比：

- AES code-only LLM 结果略高，为 99.701%，但需要更多 case。
- SHA-256 code-only 和 combined feedback 的最终 coverage 一致，说明当前 heuristic
  已经能从 structural gap 中推导出足够有效的 message length 相关 case。
- code-only 场景下 functional gap 被有意忽略，报告中的 functional gap 字段不能用于判断
  UVM coverpoint 是否完成。

### 当前结论

- 三层 feedback 链路已经可以端到端运行：coverage summary -> Layer 2/3 feedback ->
  Layer 1 directives -> 新 corpus -> replay/coverage -> 对比报告。
- LLM feedback 对 AES 这类 heuristic 难以推进的 gap 有明显收益。
- SHA-256 当前规则路径已经足够强，LLM 没有进一步提高最终 coverage。
- 后续评估应增加更多 target 和轮数，并记录 per-case attribution，避免只能从 aggregate
  delta 归因。

## 2026-06-01 Functional Feedback Smoke

本节记录 2026-06-01 对 functional coverage feedback 的 smoke 评估。
实验目标是区分三件事：

- 当前 heuristic feedback 是否能提高覆盖率。
- `llm-feedback-fuzz` 在当前环境是否真的调用 LLM。
- LLM 若能基于 functional coverage gap 输出更精确 directives，是否有额外收益。

## 实验设置

所有实验使用固定 seed，并关闭随机迭代：

```sh
LIBAFL_ITERS=0
LIBAFL_MAX_SEEDS=0
LIBAFL_SEED=1
```

baseline 只包含 schema mandatory/edge cases；feedback 使用 baseline 生成的
mutation directives 重新生成 corpus 并重新采集 RTL coverage。AES 运行时使用
`EXTRA_ARGS="-Wno-UNOPTFLAT"`。

可用一键脚本复现实验并生成 JSON/Markdown 对比结果：

```sh
uv run python3 libafl_bfm_fuzz/scripts/coverage_feedback_compare.py --quiet
```

默认运行 `secworks_aes` 和 `secworks_sha256` 的三组结果：

- `baseline`：不应用 feedback directives。
- `heuristic`：使用 baseline 生成的 generic heuristic directives。
- `llm`：基于 baseline coverage summary 调用 `coverage_feedback.py --llm` 生成
  directives，再用该 directives 重放并采集 coverage。

默认 artifacts 写入
`libafl_bfm_fuzz/coverage/feedback_compare/`：

- `coverage_feedback_comparison.json`
- `coverage_feedback_comparison.md`
- 每个 target/mode 的 corpus、coverage summary、functional coverage、directives
  和命令日志。

当前环境未设置 `OPENAI_API_KEY`。因此 `--llm` 路径不会真实调用模型，实际输出为：

```text
source=heuristic; OPENAI_API_KEY not set
```

如果需要确保第三组一定是真实 LLM 结果，可设置 `OPENAI_API_KEY` 后加
`--require-real-llm`：

```sh
export OPENAI_API_KEY=...
export LANGSMITH_TRACING=true
export LANGSMITH_API_KEY=...
uv run python3 libafl_bfm_fuzz/scripts/coverage_feedback_compare.py \
  --require-real-llm \
  --llm-model gpt-4.1-mini
```

LLM feedback 通过 LangChain `ChatOpenAI` 调用模型；如果设置了 LangSmith 环境变量，
调用会以 `coverage_feedback_directives` run name 记录，并带有
`coverage-feedback` 和 target name tags。`OPENAI_BASE_URL` 与 `OPENAI_MODEL`
仍可用于切换兼容 OpenAI API 的服务和默认模型。

## 结果

下表中的 overall/toggle/branch/expr 来自
`rtl_structure_coverage` point summary；`uncovered_lines` 来自 coverage summary
中的 LCOV uncovered line entries。

| Target | Run | Cases | Functional gap | Uncovered lines | Overall | Toggle | Branch | Expr |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| secworks_aes | baseline | 13 | none | 20 | 95.818% | 95.607% | 98.508% | 84.210% |
| secworks_aes | heuristic feedback | 150 | none | 18 | 98.882% | 98.970% | 99.254% | 84.210% |
| secworks_sha256 | baseline | 6 | `message_length`: `1..15`, `32..55`, `56+` | 34 | 98.795% | 99.424% | 93.939% | 72.222% |
| secworks_sha256 | heuristic feedback | 15 | `message_length`: `32..55`, `56+` | 34 | 98.843% | 99.478% | 93.939% | 72.222% |
| secworks_sha256 | LLM-style explicit cases | 14 | none | 14 | 99.293% | 99.586% | 96.970% | 72.222% |

Verilator stdout summary showed the same broad trend:

- AES feedback improved toggle coverage from 95.6% to 99.0% and branch from 98.5%
  to 99.3%; line and expression percentages were unchanged.
- SHA heuristic feedback only moved toggle coverage from 99.4% to 99.5%; branch,
  line and expression percentages were unchanged.
- SHA LLM-style explicit cases improved branch coverage from 93.9% to 97.0%, and
  annotation coverage from 74% to 83%.

## Analysis

AES already has complete functional coverage in the current model. The generic
heuristic still helps structural coverage because it refreshes the schema space
across `op`, `key_len`, `key` patterns and `block` patterns. The remaining line
gaps are mostly MMIO readback addresses and defensive/default RTL branches that
cannot be targeted by current AES stimulus fields alone.

SHA exposes the limitation more clearly. The custom coverage model reports a
semantic `message_length` gap, but the target manifest only exposes `message` as
a variable-length hex field. Current heuristic refreshes `message_patterns`, which
mostly produces 16-byte messages and does not intentionally generate 32..55 or
56+ byte payloads. As a result, it removes the `1..15` gap but leaves longer
message-length bins uncovered.

The LLM-style explicit case experiment manually supplied what an LLM should be
able to infer from the prompt: explicit SHA messages with lengths 1, 40, 56 and
64 bytes. This hit the multi-block `next` path and removed all reported
functional gaps while also improving branch and line-entry coverage. Therefore,
LLM feedback can improve coverage for targets whose coverage gaps require
semantic interpretation not represented in the manifest schema.

## Recommendations

- Treat current `llm-feedback-fuzz` results as heuristic unless
  `OPENAI_API_KEY` is set and `llm_response.json` is produced.
- Preserve the heuristic path as a robust baseline; it is useful for AES-like
  schema-coverable gaps.
- Add a generic adapter for length-bucket coverpoints on variable-length hex
  fields, for example mapping `message_length` gaps to explicit representative
  cases. This would make the SHA improvement deterministic even without LLM.
- For true LLM evaluation, compare three corpora per target: baseline,
  heuristic feedback and LLM feedback, then replay all three with RTL coverage
  enabled.
