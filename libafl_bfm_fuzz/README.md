# LibAFL + BFM fuzz framework

This directory contains the reusable LibAFL corpus-generation and pyUVM replay
framework. Target-specific DUT behavior lives outside the framework as explicit
plugins selected by a target manifest.

Detailed architecture, module boundaries, plugin contracts, and design
constraints are documented in
[`../docs/README.md`](../docs/README.md).

## Shape

- `src/main.rs` is the binary entry point.
- `src/app.rs` is a schema-driven LibAFL corpus generator. It reads a target
  manifest, mutates generic byte inputs, decodes them through the manifest
  `[[field]]` schema, and writes JSONL cases.
- `py/fuzz_bfm/target_config.py` loads target manifests.
- `py/fuzz_bfm/corpus.py` validates JSONL cases against manifest fields.
- `py/fuzz_bfm/plugin_loader.py` dynamically loads manifest plugins.
- `py/fuzz_uvm/testbench.py` is the cocotb/pyUVM replay entry point.
- `py/fuzz_uvm/` contains reusable replay context, sequence items, sequences,
  driver/ref-model/scoreboard hooks, functional coverage, and environment
  wiring.
- `py/fuzz_feedback/` contains RTL coverage parsing, generic mutation advice,
  optional LLM calls, directive validation, and the CLI used by
  `coverage_feedback.py`.

Framework code does not include DUT-specific BFMs, reference models, vectors, or
hardcoded example paths. A DUT is added by providing a target manifest plus
driver/ref-model/coverage plugins generated or maintained outside this core.

## Target Manifest

The manifest names the replay driver and declares the semantic JSONL fields:

```toml
name = "my_dut"
toplevel = "my_dut_top"
clock = "clk"
clock_period_ns = 1.0
reset = "reset_n"
driver = "my_project.my_driver:MyDriver"
ref_model = "my_project.my_ref_model:MyRefModel"
scoreboard = "fuzz_uvm.scoreboards:ResultScoreboard"

[signals]
clk = "clk"
reset_n = "reset_n"

[[field]]
name = "op"
kind = "enum"
choices = ["read", "write"]

[[field]]
name = "addr"
kind = "int"
min = 0
max = 4095

[[field]]
name = "payload"
kind = "hex"
hex_len = 16
```

Supported field validators are `int`, `enum`, `hex`, and `any`. `hex` fields
may use `hex_len` or `hex_len_by` to express fixed and selector-dependent byte
lengths.

## Generate Corpora

Use either `TARGET_CONFIG` or a file under `targets/<target>.toml`:

```sh
make -C libafl_bfm_fuzz TARGET=my_dut TARGET_CONFIG=/path/to/my_dut.toml generate-corpus
```

The JSONL cases are written to `libafl_bfm_fuzz/coverage/<target>_corpus.jsonl`
unless `FUZZ_CORPUS` is set.

## Replay Against RTL

Provide the target manifest, DUT source list, top-level module, and any extra
Python import path needed by your plugins:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz \
  TARGET=my_dut \
  TARGET_CONFIG=/path/to/my_dut.toml \
  VERILOG_SOURCES="/path/to/rtl/a.v /path/to/rtl/b.v" \
  TOPLEVEL=my_dut_top \
  EXTRA_PYTHONPATH=/path/to/plugin/python \
  sim
```

The replay driver plugin implements the simple `reset()` and `execute(case)`
protocol. Optional ref-model and scoreboard plugins fill expected values and own
pass/fail policy.

## Validate Framework Pieces

```sh
make -C libafl_bfm_fuzz check
```

`check` runs Rust unit tests and Python syntax checks. Corpus and UVM replay
checks require a target manifest and, for simulation, the RTL source list.

## Coverage-Guided Feedback

The framework can run Verilator coverage, summarize uncovered RTL, optionally
ask an LLM for mutation guidance, and feed generic directives back into LibAFL:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz \
  TARGET=my_dut \
  TARGET_CONFIG=/path/to/my_dut.toml \
  VERILOG_SOURCES="/path/to/rtl/a.v /path/to/rtl/b.v" \
  TOPLEVEL=my_dut_top \
  feedback-fuzz
```

Artifacts are written under `coverage/`, including structural RTL coverage,
UVM functional coverage, prompt payloads, and mutation directives. Functional
coverage defaults to `coverage/<target>_uvm_functional_coverage.json`; override
`UVM_FUNCTIONAL_COVERAGE_OUT` to write it elsewhere. The feedback summary
prefers that replay-exported JSON and falls back to corpus-derived schema
coverage when it is missing.
