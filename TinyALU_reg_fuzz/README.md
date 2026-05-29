# TinyALU_reg Hypothesis Fuzz

This directory adds a Hypothesis-based fuzz test without modifying the original
`pyuvm/examples/TinyALU_reg` example.

Run it from the project environment that contains Hypothesis:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_fuzz
```

Useful knobs:

```sh
FUZZ_SEED=7 FUZZ_MAX_EXAMPLES=64 UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_fuzz
FUZZ_CORPUS_OUT=/tmp/tinyalu_cases.jsonl UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_fuzz
FUZZ_REPLAY=/tmp/tinyalu_cases.jsonl UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_fuzz
FUZZ_DIRECTIVES=/tmp/mutation_directives.json UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_fuzz
```

The Makefile defaults to Verilator because this workspace exposes `verilator`
but not `iverilog`/`vvp`. Override with `SIM=icarus` if Icarus is available in
your environment.

Coverage feedback loop:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_fuzz coverage-feedback
UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_fuzz feedback-fuzz
```

`coverage-feedback` enables Verilator RTL coverage, writes annotated coverage
and lcov info under `TinyALU_reg_fuzz/coverage/`, summarizes coverage gaps into
`coverage_summary.json`, creates an LLM handoff prompt in `llm_prompt.json`, and
emits executable mutation directives in `mutation_directives.json`.

`feedback-fuzz` immediately feeds those directives back into Hypothesis via
`FUZZ_DIRECTIVES`.

The fuzz generator emits legal semantic cases only: `A` and `B` are 8-bit
operands, and `op` is one of the legal TinyALU operations. The pyUVM sequence
turns each case into a semantic BFM client call, so register writes, start/done
polling, and result reads remain hidden from the generator.
