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
- `mutation_planner.py`：将清晰 `rtl_gap` 转换为 mutation directives，将复杂 gap
  压缩为 LLM 输入。
- `feedback_loop.py`：实现 Layer 2 per-gap feedback、Layer 3 mutation direction
  feedback 和 directives 权重/状态更新。
- `advisors.py`：生成 generic directives 或调用 LLM。
- `cli.py`：命令行入口。

`py/connector_observe/`

- `connector.py`：同步/异步 connector wrapper 和 observation context。
- `schema.py`：connector event 与 artifact reference schema。
- `observers.py`：JSONL observer、monitoring observer、async observer。

`py/fuzz_pipeline/`

- `topology.py`：harness、coverage feedback 和 full fuzz topology。
- `orchestrator.py`：`StepSpec` / `PipelineContext` / `PipelineOrchestrator`，负责把
  纯逻辑 handler、外部命令和 artifact role contract 编排成可观测步骤。
- `replay_orchestrator.py`：`ReplayPipelineOrchestrator`，负责 pyUVM replay 各组件边界的
  StepSpec 编排。
- `run_plan.py`：`RunStage` / `RunPlan` / `RunPlanExecutor`，负责 run/campaign stage
  contract、静态依赖校验、runtime result readiness 和 stage policy 执行。
- `run_profiles.py`：默认 run/campaign profile，使用 stage name 列表描述
  `feedback_fuzz`、`no_feedback`、round evaluation、campaign evaluation 和
  harness optimization phase-one 与 validation DAG；后续 candidate regression backend
  在同一 validation profile 内承接 phase-three/phase-four 行为。
- `run_stage_registry.py`：把 profile 中的 stage name 解析为具体 `RunStage`，并提供
  自定义 stage 注册入口。
- `run_adapters.py`：corpus generator、UVM replay 和 coverage report 的可替换 backend
  adapter。
- `run_evaluation.py`：默认 round/campaign evaluation report adapter，以及可替换的
  `EvaluationBackends`。
- `harness_optimization.py`：从 campaign evaluation、harness evaluation、LLM dataset 和
  campaign rollup 生成 optimization task/proposal/schema decision，并支持显式 profile 下的
  sandbox apply、candidate evaluation、metric delta 和 final decision artifacts。
- `harness_candidate_regression.py`：真实 candidate validation backend，将安全 proposal
  action 子集物化成 sandbox run config；candidate action adapter 会把 `replay_probe`、
  `scoreboard_check`、`coverage_feedback_tuning` 等 safe action 转换为 sandbox overlay、
  config artifact 和 `HARNESS_*_CONFIG` make 变量，并复用现有 campaign/run 编排执行候选
  回归，生成 candidate metrics、variant ranking 和 promotion package。
- `harness_runtime_actions.py`：safe action runtime consumer，负责加载 sandbox config，
  在 replay driver、scoreboard 和 coverage feedback 业务层消费 `replay_probe`、
  `scoreboard_check`、`coverage_feedback_tuning`，并把执行指标写入
  `harness_runtime_metrics`。
- `run_orchestrator.py`：`FuzzRunOrchestrator`，负责顶层 corpus generation /
  validation、coverage replay、Verilator coverage report、coverage feedback、feedback
  replay、round manifest 和 round evaluation 的 profile 编排。
- `campaign_orchestrator.py`：`CampaignOrchestrator` 和 `CampaignRoundScheduler`，负责
  多 mode/round 展开、上一轮 manifest state 接线、campaign manifest 和 campaign
  evaluation。
- `observation.py`：`ObservationRuntime`，统一 CLI observer/context 创建。
- `harness.py`：Makefile 命令包装和 pyUVM replay 共用的 observation helper。
- `coverage_feedback.py`：带 connector 的 coverage feedback pipeline。
- `observable.py` 位于 `py/fuzz_uvm/`，集中封装 pyUVM replay driver/ref-model、
  scoreboard 和 functional coverage adapter；adapter 委托 `ReplayPipelineOrchestrator`
  执行 connector step。

## Corpus Generation Flow

1. `make generate-corpus` 通过 `scripts/run_fuzz_pipeline.py generate-corpus` 调用
   `FuzzRunOrchestrator`；该 orchestrator 会构造 external command `StepSpec` 运行
   Rust binary，并观测 `corpus_generator_to_corpus`。
2. Rust 读取 target manifest。
3. LibAFL 产生 byte input。
4. `src/app.rs` 根据 field schema 解码 semantic case。
5. 合并 schema edge、directed 和 LibAFL corpus cases。
6. 写出 JSONL。
7. `FuzzRunOrchestrator` 随后按同一 manifest 校验 JSONL，并观测
   `corpus_to_validation`。

`scripts/run_fuzz_pipeline.py generate-corpus` 也可独立调用。若传入 `--cwd`，
Rust generator 在该目录下执行；`FuzzRunOrchestrator` 会把相对的 corpus、
target manifest、directives 和 LibAFL manifest 路径解析到同一目录下，避免
generation 与 validation 看到不同 artifact。`--topology-out` 仍按 Python CLI
调用者的路径解析。

Makefile 会把 `CARGO` 作为一个完整 wrapper 字符串传给 pipeline runner；
`FuzzRunOrchestrator` 再按 shell token 规则拆成 argv，因此 `CARGO='cargo +nightly'`
或 `CARGO='sccache cargo'` 可用于选择 toolchain/wrapper。

## Replay Flow

1. cocotb 加载 `fuzz_uvm.testbench`。
2. `ReplayContext.from_env()` 通过 `ReplayPipelineOrchestrator` 加载 manifest 和 corpus，
   并观测 `corpus_to_replay_context`。
3. `LibAflUvmReplayTest` 启动 manifest 指定的 clock。
4. `CorpusReplaySequence` 委托 `ReplayPipelineOrchestrator` 顺序发送 case，并观测
   `case_to_replay_driver`。
5. `ReplayDriver` 通过 `ObservableReplayDriverAdapter` 调用目标 driver plugin，并观测
   `manifest_to_replay_driver`、`driver_reset_to_dut` 和 `case_to_dut`。
6. 可选 ref model 填充 expected，并观测 `manifest_to_ref_model` 和
   `case_to_ref_model`。
7. Scoreboard 通过 `ObservableScoreboardAdapter` 检查 result，并观测
   `manifest_to_scoreboard`、`driver_to_scoreboard` 和 `scoreboard_to_report`。
8. Functional coverage subscriber 通过 `ObservableCoverageAdapter` 输出 JSON summary，并观测
   `manifest_to_functional_coverage`、`driver_to_functional_coverage` 和
   `functional_coverage_to_summary`。默认路径由 `ReplayPipelineOrchestrator` 解析为
   `coverage/<target>_uvm_functional_coverage.json`，可由
   `UVM_FUNCTIONAL_COVERAGE_OUT` 覆盖。

## Coverage Feedback Flow

1. `make coverage-report` 通过 `scripts/run_fuzz_pipeline.py coverage-report`
   调用 `FuzzRunOrchestrator`。该 orchestrator 先显式执行 corpus generation 和
   validation，再用 `corpus_to_uvm_replay_process` 包装带 RTL coverage 的 pyUVM
   replay，最后用
   `rtl_coverage_to_coverage_report` 包装 `verilator_coverage --annotate` 和
   `--write-info`。
2. Verilator coverage 输出 `.dat` / `.info`。
3. `rtl_structure_coverage.py` 将 `.dat` / `.info` 归一化为 structured
   `CoverageExport(domain="rtl_structure")`。
4. `rtl_gap.py` 从 uncovered structural coverage points 聚合 `rtl_gap_summary`，
   包含 gap id、源码上下文、evidence 和 advisor hints。
5. `make coverage-feedback` 通过 `scripts/run_fuzz_pipeline.py coverage-feedback`
   调用 `FuzzRunOrchestrator`，再委托 `CoverageFeedbackPipeline` 执行 feedback 内部
   connector；`make feedback-fuzz` 使用同一个 runner 继续串起反馈 replay。
6. `coverage.py` 汇总 uncovered line、structured coverage export、`rtl_gap_summary`、
   functional coverage 和 stimulus summary。
   它优先读取 UVM replay 导出的 functional coverage JSON；如果文件不存在，则回退到
   从 JSONL corpus 重新计算 schema-level functional coverage。
7. `feedback_loop.py` 可根据上一轮 summary/directives/state 生成 Layer 2 gap feedback
   和 Layer 3 mutation feedback；没有上一轮输入时保持单轮旧行为。
8. `advisors.py` 生成 generic directives。当前优先使用 functional coverage 中的
   uncovered field/coverpoint；`mutation_planner.py` 会把清晰 `rtl_gap` 转换为
   structural directives，并把复杂 gap 放入 LLM prompt；最后回退到 sparse stimulus
   field heuristic。也可调用 LLM 生成 directives。
9. `feedback-fuzz` 在 coverage feedback 后显式调用 corpus generator 读取 directives，
   生成并校验 `<target>_feedback_corpus.jsonl`；随后通过 run-level
   `directives_to_feedback_replay` connector replay 该 feedback corpus。单独跑下一轮时，
   `generate-corpus` 仍可通过 `--directives` 读取 directives。
10. 同一轮结束时，`round_artifacts_to_round_manifest` 写出
    `<target>_round_manifest.json`，记录本轮输入、输出 artifact、coverage/feedback
    摘要、connector observation 路径和各外部命令 return code。
11. `make feedback-campaign` / `run_fuzz_pipeline.py feedback-campaign` 使用
    `CampaignOrchestrator` 串起多个 mode/round，并通过
    `round_manifest_to_campaign_manifest` 写出 `campaign_manifest.json`。feedback mode
    的下一轮从上一轮 `round_manifest.artifacts` 读取 canonical summary/directives/state
    路径，不再从 round 目录命名规则反推 previous state。
12. `feedback-fuzz` 可通过 `--run-plan-profile` 选择 run DAG，通过 `--evaluation-out`
    追加 `round_evaluation`。`feedback-campaign` 可通过 `--run-plan-profile` 选择每轮
    DAG，通过 `--campaign-plan-profile` 选择 campaign DAG，通过 `--round-evaluation`
    和 `--campaign-evaluation-out` 生成 round/campaign evaluation report。
13. 默认 `feedback_fuzz` 和 `no_feedback` 行为保持与迁移前主流程一致；新增 stage 应通过
    registry/profile 插入，并由 `RunStage` contract 声明 result/artifact 依赖。
14. 显式 harness optimization validation profile 可注入
    `HarnessCandidateRegressionBackend`。backend 只在 optimization sandbox 下物化 candidate
    action overlay、per-action config、可选 mutation directives、variant ranking 和
    review-only promotion package，并在 candidate run 内通过 `HARNESS_RUNTIME_METRICS_OUT`
    汇总 runtime action execution metrics；默认主流程和源码主线不受影响。

结构化 coverage export 和 `rtl_gap` 的 schema 见
[Coverage Feedback 设计](coverage_feedback_design.md)。

## Connector Observation Flow

设置以下环境变量可为 corpus generation、validation、replay 和 feedback 导出观测：

```sh
CONNECTOR_OBSERVE_OUT=coverage/connector_events.jsonl
CONNECTOR_MONITOR_OUT=coverage/component_monitor.json
CONNECTOR_TOPOLOGY_OUT=coverage/component_topology.json
CONNECTOR_OBSERVE_RUN_ID=my_run
CONNECTOR_OBSERVE_ROUND_ID=round_00
```

事件文件记录每条 connector 的 started/finished/failed，monitor 文件聚合每个
connector 的 started、finished、failed、duration 和最近一次 metrics。完整拓扑名为
`libafl_bfm_fuzz`，包含 harness、coverage feedback 和 run orchestration。

详见 [Connector Observability 架构](connector_observability.md)。

## 当前 replay 粒度

当前默认粒度是：

```text
one JSONL line -> one FuzzSeqItem -> one driver.execute(case)
```

复杂初始化、多阶段事务、burst、stateful flow 和 monitor-driven checking 应通过
后续 sequence plugin 或目标自定义 driver 实现。
