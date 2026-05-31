# LibAFL + BFM fuzz framework

This folder upgrades the TinyALU-specific LibAFL prototype into a reusable
corpus-generation and BFM-replay framework.

## Shape

- `src/main.rs` is only the binary entry point; `src/app.rs` owns the current
  LibAFL corpus generator.
- `targets/*.toml` declares target metadata, clock/signal mapping, corpus field
  validation, and Python driver/coverage plugins for each DUT.
- `py/fuzz_bfm/corpus.py` validates the shared JSONL schema.
- `py/fuzz_bfm/target_config.py` loads target manifests.
- `py/fuzz_bfm/plugin_loader.py` dynamically loads manifest plugins.
- `py/fuzz_uvm/testbench.py` is the cocotb/pyUVM replay entry point;
  `py/fuzz_bfm/replay_testbench.py` remains as a compatibility wrapper.
- `py/fuzz_bfm/tinyalu_driver.py` drives the existing TinyALU BFM.
- `py/fuzz_bfm/mem_bus_bfm.py` drives secworks-style memory-mapped wrappers.
- `py/fuzz_bfm/aes_driver.py` and `py/fuzz_bfm/sha256_driver.py` adapt AES/SHA256
  semantic cases onto that memory-mapped BFM.
- `py/fuzz_uvm/` contains the reusable pyUVM replay layers:
  `context.py` loads target/corpus configuration, `transactions.py` defines
  replay records and sequence items, `sequences.py` turns corpus cases into UVM
  traffic, `components.py` owns driver/scoreboard/coverage components, `env.py`
  wires the reusable environment, and `replay.py` remains the thin test entry.
- `py/fuzz_feedback/` contains coverage parsing, heuristic advisors, optional
  LLM calls, directive validation, and the CLI used by `coverage_feedback.py`.
- `py/fuzz_feedback/rtl_structure_coverage.py` defines RTL structural coverage
  separately from future UVM functional coverage.

The replay path no longer hardcodes `tinyalu/aes/sha256` driver or coverage
classes in the testbench. Adding a DUT now starts with a target manifest, a
driver plugin, and optionally a functional coverage plugin; the cocotb entry
point stays unchanged.

The cocotb entry point is now a thin compatibility wrapper over the generic
`fuzz_uvm` environment. Existing target drivers still implement the simple
`reset()`/`execute(case)` protocol, while the UVM layer provides reusable
sequence, driver, scoreboard, clocking, and functional coverage structure for
future multi-step sequences and LLM-generated directives.

## Generate corpora

```sh
make -C libafl_bfm_fuzz TARGET=tinyalu generate-corpus
make -C libafl_bfm_fuzz TARGET=aes generate-corpus
make -C libafl_bfm_fuzz TARGET=sha256 generate-corpus
```

The JSONL cases are written to `libafl_bfm_fuzz/coverage/<target>_corpus.jsonl`.

## Replay against RTL

Run from an environment that has cocotb, pyUVM, Verilator, and Rust available:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz TARGET=tinyalu sim
UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz TARGET=aes sim
UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz TARGET=sha256 sim
```

TinyALU is the first smoke-test target because it reuses the existing project
BFM. AES and SHA256 use the shared memory-mapped BFM and compare against Python
oracles.

## Validate framework pieces

```sh
make -C libafl_bfm_fuzz check-all
```

`check-all` runs the Rust unit tests, Python syntax checks, and corpus generation
for TinyALU, AES, and SHA256.

Run the pyUVM replay smoke across all built-in DUTs:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz check-uvm
```

`check-uvm` generates a small corpus for TinyALU, AES, and SHA256, then replays
each corpus through the same generic pyUVM environment.

## Coverage-guided feedback

The framework can run Verilator coverage, summarize uncovered RTL, ask an LLM
for mutation guidance, and feed the resulting directives back into LibAFL.

Heuristic-only feedback, which works without an API key:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz TARGET=aes feedback-fuzz
UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz TARGET=sha256 feedback-fuzz
```

LLM-assisted feedback:

```sh
OPENAI_API_KEY=... OPENAI_MODEL=... \
  UV_CACHE_DIR=/tmp/uv-cache uv run make -C libafl_bfm_fuzz TARGET=aes llm-feedback-fuzz
```

Artifacts are written under `coverage/`:

- `<target>_coverage_summary.json`: compact uncovered-line and corpus summary.
  It includes `rtl_structure_coverage` with line, branch, expression, toggle,
  FSM, and user coverage slots. The kinds present depend on what Verilator
  emitted for the DUT. It also includes `uvm_functional_coverage`, a semantic
  bin/cross summary derived from the replay corpus.
- `<target>_uvm_functional_coverage.json`: functional coverage observed by the
  generic pyUVM replay subscriber during simulation.
- `<target>_llm_prompt.json`: prompt payload for offline/manual LLM review.
- `<target>_llm_response.json`: raw model response when LLM feedback is enabled.
- `<target>_mutation_directives.json`: validated directives consumed by LibAFL.

If `OPENAI_API_KEY` is missing or the model call fails, the script keeps the fuzz
loop moving by writing deterministic heuristic directives.

## Target plugin manifests

Each target manifest names the driver and the semantic JSONL fields:

```toml
name = "aes"
toplevel = "aes"
clock = "clk"
clock_period_ns = 1.0
driver = "fuzz_bfm.aes_driver:AesDriver"
coverage_model = "fuzz_uvm.functional_coverage:AesCoverageModel"

[signals]
clk = "clk"
reset_n = "reset_n"
cs = "cs"
we = "we"
address = "address"
write_data = "write_data"
read_data = "read_data"

[[field]]
name = "encdec"
kind = "enum"
choices = ["encipher", "decipher"]
```

Supported field validators are `int`, `enum`, and `hex`; `hex` fields may use
`hex_len` or `hex_len_by` to express fixed and selector-dependent byte lengths.
`clock`, `clock_period_ns`, and `[signals]` keep DUT naming out of the reusable
UVM test. Manifests may also reserve optional plugin hooks such as `oracle`,
`monitor`, `coverage_model`, and `sequence_schema`; these are loaded into
`TargetConfig` for future LLM-driven sequence compilation without changing the
replay entry point.
