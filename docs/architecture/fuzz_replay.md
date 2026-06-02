# Fuzz and Replay Architecture

本文档描述 `libafl_bfm_fuzz` 的运行架构。

## 模块结构

`src/main.rs`

- Rust binary entry。
- 调用 `app::main_entry()`。

`src/app.rs`

- LibAFL corpus generator。
- 读取 target manifest 的 `[[field]]` schema。
- 将 mutational byte input 解码为 schema case。
- 合并 mandatory/schema edge cases、directed cases 和 LibAFL corpus cases。
- 写出 JSONL corpus。

`py/fuzz_bfm/`

- `target_config.py`：加载 manifest。
- `corpus.py`：校验 JSONL corpus。
- `plugin_loader.py`：加载 `module:Object` plugin。
- `bfm_base.py`：定义 `ReplayResult` 和 driver protocol。

`py/fuzz_uvm/`

- `replay.py`：pyUVM replay test 顶层。
- `context.py`：从环境变量加载 manifest 和 corpus。
- `env.py`：组装 sequencer、driver、scoreboard、coverage subscriber。
- `sequences.py`：把 corpus case 包装为 sequence item。
- `components.py`：调用 driver/ref-model/scoreboard/coverage plugin。
- `transactions.py`：定义 replay record 和 sequence item。

`py/fuzz_feedback/`

- `coverage.py`：汇总 structured coverage export、RTL gap、functional coverage 和 stimulus。
- `coverage_export.py`：定义可版本化的 normalized coverage point/export。
- `rtl_structure_coverage.py`：解析 LCOV 和 Verilator `.dat`，导出 RTL structural coverage。
- `rtl_gap.py`：从 uncovered structural coverage point 聚合结构化 `rtl_gap`。
- `advisors.py`：生成 generic directives 或调用 LLM。
- `cli.py`：命令行入口。

## Corpus Generation Flow

1. `make generate-corpus` 调用 Rust binary。
2. Rust 读取 target manifest。
3. LibAFL 产生 byte input。
4. `src/app.rs` 根据 field schema 解码 semantic case。
5. 合并 schema edge、directed 和 LibAFL corpus cases。
6. 写出 JSONL。
7. Python validator 按同一 manifest 校验 JSONL。

## Replay Flow

1. cocotb 加载 `fuzz_uvm.testbench`。
2. `ReplayContext.from_env()` 加载 manifest 和 corpus。
3. `LibAflUvmReplayTest` 启动 manifest 指定的 clock。
4. `CorpusReplaySequence` 顺序发送 case。
5. `ReplayDriver` 调用目标 driver plugin。
6. 可选 ref model 填充 expected。
7. Scoreboard 检查 result。
8. Functional coverage subscriber 输出 JSON summary。默认路径为
   `coverage/<target>_uvm_functional_coverage.json`，可由
   `UVM_FUNCTIONAL_COVERAGE_OUT` 覆盖。

## Coverage Feedback Flow

1. Verilator coverage 输出 `.dat` / `.info`。
2. `rtl_structure_coverage.py` 将 `.dat` / `.info` 归一化为 structured
   `CoverageExport(domain="rtl_structure")`。
3. `rtl_gap.py` 从 uncovered structural coverage points 聚合 `rtl_gap_summary`，
   包含 gap id、源码上下文、evidence 和 advisor hints。
4. `coverage.py` 汇总 uncovered line、structured coverage export、`rtl_gap_summary`、
   functional coverage 和 stimulus summary。
   它优先读取 UVM replay 导出的 functional coverage JSON；如果文件不存在，则回退到
   从 JSONL corpus 重新计算 schema-level functional coverage。
5. `advisors.py` 生成 generic directives。当前优先使用 functional coverage 中的
   uncovered field/coverpoint，再回退到 sparse stimulus field heuristic；后续 structural
   advisor 应消费 `rtl_gap_summary.top_gaps`。也可调用 LLM 生成 directives。
6. 下一轮 `generate-corpus` 通过 `--directives` 读取 directives。

结构化 coverage export 和 `rtl_gap` 的 schema 见
[Coverage Feedback 设计](coverage_feedback_design.md)。

## 当前 replay 粒度

当前默认粒度是：

```text
one JSONL line -> one FuzzSeqItem -> one driver.execute(case)
```

复杂初始化、多阶段事务、burst、stateful flow 和 monitor-driven checking 应通过
后续 sequence plugin 或目标自定义 driver 实现。
