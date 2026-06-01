# Coverage Feedback Evaluation

本文档记录 2026-06-01 对当前 functional coverage feedback 的 smoke 评估。
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

当前环境未设置 `OPENAI_API_KEY`。因此 `--llm` 路径不会真实调用模型，实际输出为：

```text
source=heuristic; OPENAI_API_KEY not set
```

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
