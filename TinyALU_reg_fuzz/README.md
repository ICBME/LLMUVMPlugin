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
```

The Makefile defaults to Verilator because this workspace exposes `verilator`
but not `iverilog`/`vvp`. Override with `SIM=icarus` if Icarus is available in
your environment.

The fuzz generator emits legal semantic cases only: `A` and `B` are 8-bit
operands, and `op` is one of the legal TinyALU operations. The pyUVM sequence
turns each case into the original `AluSeqItem` and calls the original
`program_alu_reg()` path, so BFM timing and register details remain hidden from
the generator.
