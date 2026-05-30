# LibAFL + BFM fuzz framework

This folder upgrades the TinyALU-specific LibAFL prototype into a reusable
corpus-generation and BFM-replay framework.

## Shape

- `src/main.rs` generates JSONL semantic corpora with LibAFL.
- `py/fuzz_bfm/corpus.py` validates the shared JSONL schema.
- `py/fuzz_bfm/replay_testbench.py` is the cocotb/pyUVM replay entry point.
- `py/fuzz_bfm/tinyalu_driver.py` drives the existing TinyALU BFM.
- `py/fuzz_bfm/mem_bus_bfm.py` drives secworks-style memory-mapped wrappers.
- `py/fuzz_bfm/aes_driver.py` and `py/fuzz_bfm/sha256_driver.py` adapt AES/SHA256
  semantic cases onto that memory-mapped BFM.

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
- `<target>_llm_prompt.json`: prompt payload for offline/manual LLM review.
- `<target>_llm_response.json`: raw model response when LLM feedback is enabled.
- `<target>_mutation_directives.json`: validated directives consumed by LibAFL.

If `OPENAI_API_KEY` is missing or the model call fails, the script keeps the fuzz
loop moving by writing deterministic heuristic directives.
