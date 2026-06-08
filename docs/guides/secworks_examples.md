# Secworks AES and SHA-256 Examples

本文档说明如何使用仓库中已有的 `example/aes` 和 `example/sha256` RTL 运行
`libafl_bfm_fuzz` 闭环 smoke。

## 已接入 Target

`libafl_bfm_fuzz/targets/secworks_aes.toml`

- Top: `aes`
- Driver: `fuzz_examples.secworks_aes:AesMmioDriver`
- Ref model: `fuzz_examples.secworks_aes:AesRefModel`
- Coverage model: `fuzz_examples.secworks_aes:AesCoverageModel`
- Case schema: `op`、`key_len`、`key`、`block`

`libafl_bfm_fuzz/targets/secworks_sha256.toml`

- Top: `sha256`
- Driver: `fuzz_examples.secworks_sha256:Sha256MmioDriver`
- Ref model: `fuzz_examples.secworks_sha256:Sha256RefModel`
- Coverage model: `fuzz_examples.secworks_sha256:Sha256CoverageModel`
- Case schema: `mode`、`message`

这些 target 使用真实 RTL replay 和 default result scoreboard。AES ref model 是本地
Python AES-128/AES-256 block cipher；SHA-224/SHA-256 ref model 使用 Python
`hashlib`。

## Corpus + Replay

AES：

```sh
AES_RTL="$(printf '%s ' /path/to/pyuvm/example/aes/src/rtl/*.v)"
uv run make -C libafl_bfm_fuzz \
  TARGET=secworks_aes \
  LIBAFL_ITERS=0 \
  LIBAFL_MAX_SEEDS=0 \
  VERILOG_SOURCES="$AES_RTL" \
  TOPLEVEL=aes \
  EXTRA_ARGS="-Wno-UNOPTFLAT" \
  sim
```

Secworks AES 的 S-box 在 Verilator 下会触发 `UNOPTFLAT` warning；示例命令用
`-Wno-UNOPTFLAT` 保持 RTL 原样运行。

SHA-256：

```sh
SHA_RTL="$(printf '%s ' /path/to/pyuvm/example/sha256/src/rtl/*.v)"
uv run make -C libafl_bfm_fuzz \
  TARGET=secworks_sha256 \
  LIBAFL_ITERS=0 \
  LIBAFL_MAX_SEEDS=0 \
  VERILOG_SOURCES="$SHA_RTL" \
  TOPLEVEL=sha256 \
  sim
```

## Coverage Feedback Loop

AES：

```sh
AES_RTL="$(printf '%s ' /path/to/pyuvm/example/aes/src/rtl/*.v)"
uv run make -C libafl_bfm_fuzz \
  TARGET=secworks_aes \
  LIBAFL_ITERS=0 \
  LIBAFL_MAX_SEEDS=0 \
  VERILOG_SOURCES="$AES_RTL" \
  TOPLEVEL=aes \
  EXTRA_ARGS="-Wno-UNOPTFLAT" \
  feedback-fuzz
```

SHA-256：

```sh
SHA_RTL="$(printf '%s ' /path/to/pyuvm/example/sha256/src/rtl/*.v)"
uv run make -C libafl_bfm_fuzz \
  TARGET=secworks_sha256 \
  LIBAFL_ITERS=0 \
  LIBAFL_MAX_SEEDS=0 \
  VERILOG_SOURCES="$SHA_RTL" \
  TOPLEVEL=sha256 \
  feedback-fuzz
```

闭环会执行：

1. manifest-driven corpus generation。
2. cocotb/pyUVM replay。
3. ref model expected 填充。
4. scoreboard actual/expected 检查。
5. UVM functional coverage JSON 导出。
6. Verilator structural coverage report。
7. mutation directives 生成。
8. 使用 directives 生成 feedback corpus 并再次 replay。

## Connector Observation

Secworks 示例也可以导出 connector 事件、monitor 和 full topology。以 SHA-256 为例：

```sh
SHA_RTL="$(printf '%s ' /path/to/pyuvm/example/sha256/src/rtl/*.v)"
uv run make -C libafl_bfm_fuzz \
  TARGET=secworks_sha256 \
  LIBAFL_ITERS=0 \
  LIBAFL_MAX_SEEDS=0 \
  COVERAGE_DIR=libafl_bfm_fuzz/coverage/sha256_observe \
  VERILOG_SOURCES="$SHA_RTL" \
  TOPLEVEL=sha256 \
  CONNECTOR_OBSERVE_OUT=libafl_bfm_fuzz/coverage/sha256_observe/events.jsonl \
  CONNECTOR_MONITOR_OUT=libafl_bfm_fuzz/coverage/sha256_observe/monitor.json \
  CONNECTOR_TOPOLOGY_OUT=libafl_bfm_fuzz/coverage/sha256_observe/topology.json \
  CONNECTOR_OBSERVE_RUN_ID=sha256_observe \
  feedback-fuzz
```

期望输出包含：

- `events.jsonl`：connector started/finished/failed 事件。
- `monitor.json`：每个 connector 的 started、finished、failed、duration 和最新 metrics。
- `topology.json`：`libafl_bfm_fuzz` full topology，覆盖 harness 和 coverage feedback。

示例 artifacts 写入 `libafl_bfm_fuzz/coverage/`，包括：

- `secworks_*_corpus.jsonl`
- `secworks_*_uvm_functional_coverage.json`
- `secworks_*_coverage_summary.json`
- `secworks_*_mutation_directives.json`
- `secworks_*_feedback_corpus.jsonl`
