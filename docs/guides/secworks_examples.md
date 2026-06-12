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
  generate-corpus

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
  generate-corpus

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

## Strict Candidate Evidence

真实 candidate evidence 回归用于判断 proposed harness action 是否能在 matched no-op
baseline 之上产生稳定、可归因的 gateable improvement。evidence profile 默认使用
`matched_baseline=1`、`paired_repeats=3`、`attribution_mode=all_actions`、
`candidate-min-improved-metrics=1`、`candidate-max-regressed-metrics=0` 和
`candidate-max-flaky-metrics=0`。

SHA-256 的 padding boundary candidate 是当前正例：在 matched no-op baseline 上稳定减少
`uncovered_line_count`，final decision 为 `accepted_for_review`。action pruning 会只保留
`boundary-message-directives`，把 probe、scoreboard record 和 feedback tuning 归为
neutral；`minimal_promotion_candidate` 精确匹配 `action_boundary-message-directives`
variant，因此是 `ready_for_review` / `validated`。

AES 的 op/key_len/key/block 边界 candidate 是 2026-06-12 的跨目标负例：候选包含
`mutation_directive_update`、`replay_probe`、`scoreboard_check` 和
`coverage_feedback_tuning` 四类非 no-op action，并在真实 RTL 上完成 matched no-op +
`paired_repeats=3` + all-actions 回归；candidate simulation 通过且没有 flaky/regressed
gateable metric，但 `uncovered_line_count` 在三对 repeat 中保持 18 -> 18，
`gateable_improved_metric_count=0`，final decision 为
`candidate_metric_improvement_below_threshold` / `rejected`。per-action attribution 显示四个
action 的 standalone effect 全部为 neutral，promotion package 因此输出空
`minimal_promotion_candidate` 并把所有 action 放入 `neutral_actions`。

这个 AES 结果不是 candidate regression 故障，而是证据系统正确拒绝了不可推广优化：
当前 AES manifest 只暴露 `op`、`key_len`、`key`、`block` 高层 transaction 字段；剩余
RTL line gap 主要是 MMIO readback 地址和 defensive/default 分支，现有 safe action DSL
无法直接生成任意 readback 地址或非法内部状态。后续若要继续改善 AES，需要先扩展
driver/manifest/action surface，而不是 promotion 这些 neutral action。

actionability-driven optimization 阶段为 AES 增加了 `mmio_readback` safe action。该 action
现在通过 harness optimization plugin registry 暴露给 LLM schema hint、sandbox apply 和
candidate regression。payload 可声明 symbolic register，例如 `ADDR_NAME0`、
`ADDR_NAME1`、`ADDR_VERSION`、`ADDR_CTRL` 和 `ADDR_STATUS`，也可声明 explicit safe read
address，例如 `ADDR_RESULT3 + 1` 的 `0x34`；`AesMmioDriver` 会在正常 encrypt/decrypt
transaction 之后执行额外 readback，并把 `mmio_readback_read_count` 等 runtime metrics
写入 candidate evidence。AES gap actionability classifier 不在核心默认 registry 中；
`secworks_aes.toml` 通过
`fuzz_examples.secworks_aes_harness_plugin:build_plugin` 显式加载目标插件，使
`candidate_gap_actionability_report` 把 AES top-level readback gap 标为
`reachable_with_mmio_readback`，把 block write out-of-range gap 标为需要 MMIO write
surface，把内部 defensive/default gap 标为需要 internal-state surface 或 waiver。
这些 report 会携带 `plugin_validation`、`plugin_provenance`、registry fingerprint 和
插件源码 hash，用于审查规则来源；同时生成
`candidate_gap_actionability_minimal_proposal`，把可安全执行的 readback recommendation
整理成下一轮最小候选。
2026-06-12 的真实 AES candidate regression 使用 matched no-op baseline、`paired_repeats=3`
和 `attribution_mode=all_actions`，将 `uncovered_line_count` 稳定从 18 降到 13，且
regressed/flaky gateable metric 均为 0。standalone attribution 显示 `mmio_readback`
有效、边界 directive action 为 neutral，因此 `candidate_promotion_package` 的
`minimal_promotion_candidate` 只保留 `mmio_readback` action。

AES evidence 路径需要保留 Verilator 参数：

```sh
--make-var EXTRA_ARGS=-Wno-UNOPTFLAT
```

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
