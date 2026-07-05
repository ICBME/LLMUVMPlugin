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
dataset -> adapter -> generation -> automation_repair -> review -> readiness -> metrics
```

Default runs are offline and do not call an LLM. Add `--with-llm` only for manual
capability testing with a configured backend.

## Metrics

Reports include schema-valid rate, claim extraction rate, covered-claim rate,
review status counts, repair status counts, automation route counts, backend
readiness counts, deterministic repair count, and crash count.
