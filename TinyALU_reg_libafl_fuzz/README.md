# TinyALU_reg LibAFL Fuzz

This directory adds a LibAFL-based fuzz flow for `pyuvm/examples/TinyALU_reg`
without importing Hypothesis.

The Rust fuzzer evolves compact three-byte inputs:

```text
[A, B, op_selector]
```

`A` and `B` are 8-bit operands. `op_selector` is mapped onto the legal TinyALU
ops `ADD`, `AND`, `XOR`, and `MUL`, so the replay corpus always stays inside the
current BFM contract.

Run the full default flow:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_libafl_fuzz
```

Useful knobs:

```sh
LIBAFL_SEED=7 LIBAFL_ITERS=512 UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_libafl_fuzz
FUZZ_CORPUS=/tmp/tinyalu_libafl_cases.jsonl UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_libafl_fuzz generate-corpus
LIBAFL_CORPUS=/tmp/tinyalu_libafl_cases.jsonl UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_libafl_fuzz sim
FUZZ_DIRECTIVES=/tmp/mutation_directives.json UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_libafl_fuzz generate-corpus
```

Validation and unit tests:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_libafl_fuzz check
```

Coverage feedback loop:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_libafl_fuzz coverage-feedback
UV_CACHE_DIR=/tmp/uv-cache uv run make -C TinyALU_reg_libafl_fuzz feedback-fuzz
```

`coverage-feedback` reuses `TinyALU_reg_fuzz/coverage_feedback.py` so existing
mutation directives remain compatible. `feedback-fuzz` passes those directives
back into the LibAFL generator.
