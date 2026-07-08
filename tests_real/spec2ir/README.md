# Spec2IR Real-Data Tests

This directory contains real-data capability tests for Spec2IR. It is separate
from `tests/`, and `pyproject.toml` keeps default pytest discovery on unit tests
only.

## Dataset

The first supported dataset is VerilogEval spec-to-RTL:

```bash
example/verilog-eval/dataset_spec-to-rtl
```

Override the path with:

```bash
SPEC2IR_VERILOGEVAL_ROOT=/path/to/dataset_spec-to-rtl
```

## Pytest

Run a small offline smoke/aggregate pass:

```bash
uv run pytest tests_real/spec2ir -m real_data --spec2ir-real-data-limit=10 -q
```

Run optional LLM smoke tests explicitly:

```bash
SPEC2IR_REALDATA_ENABLE_LLM=1 uv run pytest tests_real/spec2ir -m "real_data and llm" -q
```

The LangChain backend automatically loads `.env` from the repository root. It
uses `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL`; `OPENAI_USER_AGENT`
can override the default `RTLAgent/Spec2IR` header for OpenAI-compatible
gateways that filter SDK default user agents.

When `LANGSMITH_TRACING=true`, the backend relies on LangChain/LangSmith's normal
environment-variable based tracing. It does not proactively query or create
LangSmith projects before invoking the model.

Real-data LLM tests default to no SDK retries so availability failures surface
quickly. Tune them with:

```bash
SPEC2IR_REALDATA_LLM_MODEL=gpt-5.5 \
SPEC2IR_REALDATA_LLM_TIMEOUT=120 \
SPEC2IR_REALDATA_LLM_MAX_RETRIES=0 \
SPEC2IR_REALDATA_ENABLE_LLM=1 \
uv run pytest tests_real/spec2ir -m "real_data and llm" -q
```

LLM-enabled real-data runs are strict. The runner first builds a deterministic
rule-based draft, then calls the `LLMPlugin` agent through `Spec2IRHarness`.
If the backend cannot be created, the LLM request fails, or the backend returns
no response, the runner raises `RealDataLLMRuntimeError`. This keeps real LLM
smoke tests from passing when the LLM was not actually used.

## Batch Evaluation

Write a JSON report:

```bash
uv run python -m tests_real.spec2ir.run_eval \
  --dataset verilogeval \
  --root example/verilog-eval/dataset_spec-to-rtl \
  --limit 50 \
  --out runs/spec2ir_real/verilogeval_50.json
```

The runner uses this stage graph:

```text
dataset -> adapter -> generation -> llm_agent? -> automation_repair -> review -> readiness -> metrics
```

Default runs are offline and do not call an LLM. Add `--with-llm` only for manual
capability testing with a configured backend; LLM availability failures abort the
run instead of being recorded as successful offline fallbacks.

## Metrics

Reports include schema-valid rate, claim extraction rate, covered-claim rate,
review status counts, repair status counts, automation route counts, backend
readiness counts, deterministic repair count, crash count, and LLM-effective
case count.
