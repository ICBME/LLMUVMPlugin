# Connector Observability Architecture

本文档描述 `libafl_bfm_fuzz` 的 connector 观测机制。它用于把 corpus generation、
validation、pyUVM replay、scoreboard、functional coverage 和 coverage feedback
这些组件以显式连接边组织起来，并导出事件、监控汇总和拓扑。

## 目标

- 让 harness/fuzz 闭环的每个关键组件边界可观测。
- 不改变主链路执行结果；observer 异常默认不会影响 fuzz/replay。
- 在未启用 observer 时保持低开销快速路径。
- 让多进程 Makefile 流程和 pyUVM 协程流程使用同一套事件 schema。
- 为后续评测 harness 效果提供统一的事件、metrics 和 artifact 引用。

## 核心模块

`py/connector_observe/connector.py`

- 定义 `Connector` 和 `ObservationContext`。
- `Connector.run()` 包装同步函数。
- `Connector.run_async()` 包装 pyUVM/cocotb 协程。
- 每条 connector 会发出 `connector.started`、`connector.finished` 或
  `connector.failed` 事件。

`py/connector_observe/schema.py`

- 定义事件 schema 和 `ArtifactRef`。
- artifact 可记录 role、path、size、mtime 和可选 sha256。

`py/connector_observe/observers.py`

- `JsonlObserver`：写出 JSONL 事件。
- `MonitoringObserver`：聚合 connector 健康状态和最新 metrics。
- `AsyncObserver`：异步写事件，避免阻塞主链路。
- `CompositeObserver`：同时写事件和 monitor。
- `observer_from_env()`：从环境变量构建 observer。

`py/connector_observe/trace.py`

- 提供可复用的 connector event JSONL 读取、JSON artifact 读取、`span_id` / status
  提取和 trace quality 分析。
- 只理解通用 connector event 语义，不包含 UVM-fuzz、coverage、case 或 directive
  业务判断。

`py/fuzz_pipeline/topology.py`

- 定义 `PipelineTopology`、`ComponentNode`、`ConnectorEdge`。
- `HARNESS_TOPOLOGY` 描述 corpus/replay/scoreboard/coverage 组件。
- `COVERAGE_FEEDBACK_TOPOLOGY` 描述 coverage feedback 与三层反馈组件。
- `FULL_FUZZ_TOPOLOGY` 合并为完整 `libafl_bfm_fuzz` 拓扑。
- `ConnectorEdge` 可声明 `input_roles`、`output_roles` 和 `required`，作为
  orchestration 层的最小 artifact contract。

`py/fuzz_pipeline/orchestrator.py`

- 定义 `StepSpec`、`StepPolicy`、`PipelineContext` 和 `PipelineOrchestrator`。
- 负责按 topology 创建 connector、校验 connector 名称和 role contract、执行 handler、
  存储 step result、汇总 metadata/metrics/artifact refs，并导出 topology JSON。
- 支持同步 `run()` / `run_step()` 和异步 `run_async()` / `run_step_async()`。
- `external_command_step()` 用于把 Rust binary、shell command 等外部进程纳入同一套
  connector 观测。

`py/fuzz_pipeline/replay_orchestrator.py`

- 定义 `ReplayPipelineOrchestrator`，把 pyUVM replay 的 context load、sequence、
  driver、ref-model、scoreboard 和 functional coverage 边界映射为 `StepSpec`。
- 维护 replay 期间共享的 `PipelineContext`，统一写入 `target_manifest`、`corpus` 和
  `functional_coverage` artifact roles。

`py/fuzz_pipeline/run_orchestrator.py`

- 定义 `FuzzRunOrchestrator`，用于顶层 fuzz harness 阶段编排。
- 通过 run-level `RunPlan` 执行 corpus generation、corpus validation、coverage replay、
  Verilator coverage report、coverage feedback、feedback corpus generation/validation、
  feedback replay、round manifest 和可选 round evaluation。
- 保留 `generate_and_validate()`、`coverage_run_pipeline()`、`feedback_fuzz()` 等入口，
  但实际顺序由 profile/stage registry 决定。

`py/fuzz_pipeline/campaign_orchestrator.py`

- 定义 `CampaignOrchestrator` 和 `CampaignRoundScheduler`。
- `CampaignRoundScheduler` 负责 mode/round 展开、上一轮 manifest 状态读取和每轮
  `FuzzRunConfig` 构造。
- `CampaignOrchestrator` 负责 campaign-level plan、`campaign_manifest`、
  `campaign_evaluation` 和 connector 包装。

`py/fuzz_pipeline/run_plan.py`

- 定义 `RunStage`、`RunPlan` 和 `RunPlanExecutor`。
- `RunStage` 声明 stage result/artifact contract 和基础执行策略；`RunPlan` 在执行前
  做静态校验；`RunPlanExecutor` 按 policy 执行并记录 fail-open stage error。

`py/fuzz_pipeline/run_profiles.py`

- 定义 `RunPlanProfile`、默认 run profile 和默认 campaign profile。
- profile 用 stage name 列表描述 DAG，并可声明 mode 约束和 stage policy override。

`py/fuzz_pipeline/run_stage_registry.py`

- 定义 `RunStageRegistry`，把 profile 中的 stage name 解析成具体 `RunStage`。
- registry 负责 unknown stage、重复注册和 factory 返回错误 stage name 的早期失败。

`py/fuzz_pipeline/run_adapters.py`

- 定义 `RunBackends` 和 corpus/replay/coverage report backend protocol。
- 默认 adapter 继续调用当前 cargo、make/cocotb 和 `verilator_coverage`，测试或后续
  agentic harness backend 可以替换这些实现。

`py/fuzz_pipeline/run_evaluation.py`

- 定义 `RunEvaluationAdapter`、`CampaignEvaluationAdapter` 和 `EvaluationBackends`。
- round/campaign evaluation 都是可替换 backend，并通过 registry stage 接入主 DAG。

`py/fuzz_pipeline/harness_records.py`

- 把通用 connector final event 投影为 harness execution record。
- 负责提取 run/round/stage、artifact evidence 等 harness 级字段，但不做评测判断。

`py/fuzz_pipeline/harness_metadata.py`

- 定义可注入的 `HarnessMetadataExtractorProtocol`。
- 默认 `UvmFuzzMetadataExtractor` 负责 UVM-fuzz case/directive/corpus hash 归因规则，
  包括把 pyUVM replay `origin` 作为缺省 `directive_id`。

`py/fuzz_pipeline/harness_analysis.py`

- 定义可注入的 `HarnessAnalyzer` protocol。
- 默认 `UvmFuzzHarnessAnalyzer` 负责 UVM-fuzz 业务评测，包括 connector/module、
  failure cluster、case/directive、coverage/feedback 和 optimization hints 聚合。

`py/fuzz_pipeline/harness_llm_tasks.py`

- 负责把 execution records 和 evaluation report 转换为 LLM optimization dataset。
- 后续可在这里扩展 task-level prompt/evidence/replay command，而不影响 trace core。

`py/fuzz_pipeline/harness_optimization.py`

- 定义 harness LLM optimization 的结构化 artifact：第一阶段包括 optimization task、
  proposal 和 schema decision；第二阶段包括 sandbox apply、candidate evaluation、
  metric delta 和 final decision；第三阶段让 candidate evaluation 可接入真实 sandbox
  regression backend。
- `HarnessOptimizationAdapter` 从 campaign evaluation、harness evaluation、LLM dataset
  和 campaign rollup 构造 task；optimizer backend 只返回 proposal，不直接修改源码或
  harness artifact。
- 默认 `NoopHarnessOptimizerBackend` 生成 schema-valid no-op proposal；
  decision stage 只做 schema-level accept/reject，并将 `application_status` 标为
  `not_applied`。
- 第二阶段 profile 使用 sandbox-only apply：安全 action 子集会被转换为
  `harness_optimization_sandbox` 下的候选 artifact；`ref_model_patch` 等潜在源码修改
  先标为 unsafe/skipped。默认 `NoopHarnessCandidateEvaluationBackend` 只写
  `not_run` validation report，真实候选回归可通过 backend 注入。

`py/fuzz_pipeline/harness_candidate_regression.py`

- 定义 `HarnessCandidateRegressionBackend`、`CandidateRegressionSettings` 和
  `CandidateAcceptanceThresholds`，并公开 `CandidateActionAdapter` /
  `JsonConfigActionAdapter` 作为 safe action 到 sandbox overlay 的扩展点。
- backend 从 optimization task 指向的 baseline campaign manifest 读取配置，在 sandbox 下
  物化 `candidate_regression_config`、`candidate_action_overlay`、per-action config artifact
  和可选 `candidate_mutation_directives`，然后用内部 `CampaignOrchestrator` 跑
  `campaign_with_evaluation` candidate campaign。默认 adapter 会为
  `replay_probe`、`scoreboard_check`、`coverage_feedback_tuning`、
  `stimulus_generation_hint` 和 `documentation_note` 写出 JSON config，并通过
  `HARNESS_*_CONFIG` 传给 sandbox run。
- candidate evaluation report 会带上 baseline/candidate metrics、candidate campaign
  artifacts、adapter metrics、variant ranking、promotion package 和 acceptance thresholds；
  final decision 只给出 review 级结论，不修改源码主线。

`py/fuzz_pipeline/harness_runtime_actions.py`

- 定义 safe action runtime config schema、loader 和 metrics writer。
- `replay_probe` 由 pyUVM replay driver adapter 消费，记录 case/result 字段采样和
  Python runtime 无法直接采样的 signal request；`scoreboard_check` 由 scoreboard adapter
  消费，记录额外 check 的 pass/fail/enforced failure；`coverage_feedback_tuning` 由
  `CoverageFeedbackPipeline` 消费，用于限制 gap 选择、调整 directive weight，并写出
  `harness_runtime_metrics`。
- 默认没有 `HARNESS_*_CONFIG` / `HARNESS_RUNTIME_METRICS_OUT` 时不加载 runtime action，
  因此主 `feedback_fuzz` / `no_feedback` 流程行为不变。

`py/fuzz_pipeline/harness_rollup.py`

- 负责把 campaign manifest、harness execution records 和 harness evaluation 聚合为
  campaign-level trace rollup。
- 输出跨 round 的 coverage trend、failure trend、case effectiveness 和 directive
  effectiveness，供后续 LLM 自动优化 harness 使用。

`py/fuzz_pipeline/harness_trace.py`

- 保留 `HarnessTraceBuilder`、`HarnessTraceOutputs` 和 `HarnessTraceResult` 兼容入口。
- 作为 facade 串联 trace core、record projector、业务 analyzer 和 LLM dataset builder；
  projector、analyzer 和 dataset builder 都通过 protocol 支持注入替换。

`py/fuzz_pipeline/observation.py`

- 定义 `ObservationRuntime`，统一 CLI 入口的 observer/context/topology_out 创建与关闭。

`py/fuzz_pipeline/harness.py`

- 为 Makefile 命令包装和 observation context 提供通用 helper。
- 负责从环境变量创建 observation context、导出拓扑、包装外部命令。

`scripts/run_fuzz_pipeline.py`

- 顶层 pipeline CLI。`generate-corpus` 子命令通过 `FuzzRunOrchestrator` 串联
  corpus generation 和 corpus validation。
- `generate-corpus --cwd DIR` 会让 Rust generator 在 `DIR` 内执行；相对的 corpus、
  target manifest、directives 和 LibAFL manifest artifact 路径也按 `DIR` 解析，
  保证 generation 与 validation 使用同一组文件。
- `--cargo` 可接收 wrapper 字符串，例如 `cargo +nightly` 或 `sccache cargo`，
  orchestrator 会拆成 subprocess argv。

`scripts/run_connector.py`

- 通用命令包装入口，保留给尚未拥有专用 orchestrator 的外部进程。

`py/fuzz_uvm/observable.py`

- 提供 `ObservableReplayDriverAdapter`、`ObservableScoreboardAdapter` 和
  `ObservableCoverageAdapter`。
- `ReplayPluginBundle` 只负责构建 driver/ref-model/scoreboard/coverage 业务插件；
  `ReplayStageAdapter` 负责把 build/reset/execute/predict/check/sample/export 包装成
  `ReplayPipelineOrchestrator` step。
- pyUVM component 通过 adapter 调用 driver/ref-model/scoreboard/coverage；component 不直接
  读取 connector env，也不决定 functional coverage artifact 路径。

## 编排契约

新接入组件应遵循以下分层：

- 层实现只保留纯逻辑、插件调用或 pyUVM 行为，不直接创建 connector，也不直接写
  observer 路径。
- 独立编排层创建 `StepSpec`，决定步骤顺序、connector 名称、输入输出 artifact role、
  metrics、metadata、失败策略和 timeout。
- `PipelineContext.values` 保存前序 step 的内存结果；`PipelineContext.artifacts` 保存
  role 到路径的映射；`PipelineContext.metadata` 保存 run/target/case 等上下文。
- 每个 `StepSpec.connector` 必须存在于 `PipelineTopology`；step 声明的
  `input_roles`/`output_roles` 必须覆盖 `ConnectorEdge` 的 contract。
- 通用 `scripts/run_connector.py` 会校验 CLI 传入的 `--from-layer` / `--to-layer`
  与 topology 中的 connector endpoint 一致。
- observer 仍默认 fail-open；只有 `STRICT_OBSERVATION=1` 或 step policy 关闭
  `fail_open_observation` 时，观测失败才会影响主链路。

当前迁移状态：

- corpus generation、corpus validation 已由 `FuzzRunOrchestrator` 编排。
- `coverage-run` / `coverage-report` 已由 `FuzzRunOrchestrator` 通过 run-level
  connector 编排；`coverage-feedback` 已由 `FuzzRunOrchestrator` 统一入口委托到
  `CoverageFeedbackPipeline`；`feedback-fuzz` 已由 run-level
  `directives_to_feedback_replay` connector 串起 directives + feedback corpus replay；
  Makefile 保留为薄 wrapper。
- coverage feedback 内部三层 pipeline 和离线三层 feedback evaluation 已由
  `PipelineOrchestrator` 编排。
- run/campaign 调度框架已升级为 profile + registry + stage contract：默认
  `feedback_fuzz`、`no_feedback` 行为保持不变，`round_evaluation` 和
  `campaign_evaluation` 可通过 profile/CLI/Makefile 插入，执行 backend 可由
  `RunBackends` / `EvaluationBackends` 替换。
- Harness LLM Optimization 第一阶段已作为显式 campaign profile 接入：
  `campaign_with_evaluation_and_optimization` 在 campaign evaluation 后生成
  `harness_optimization_task`、`harness_optimization_proposal` 和
  `harness_optimization_decision`。默认 campaign profile 不启用优化 stage。
- Harness LLM Optimization 第二阶段已作为更长的显式 campaign profile 接入：
  `campaign_with_evaluation_and_optimization_validation` 在第一阶段之后追加 sandbox
  apply、candidate evaluation、metric delta 和 final decision。该 profile 只写 sandbox
  artifact 与 review decision，不修改源码主线。
- Harness LLM Optimization 第三阶段已提供真实 candidate regression backend：
  显式注入 `HarnessCandidateRegressionBackend` 后，candidate evaluation stage 会复用现有
  campaign/run 编排执行 sandbox candidate campaign，并按阈值生成 final decision。
- Harness LLM Optimization 第四阶段已补齐 candidate action adapter 层：
  `replay_probe`、`scoreboard_check`、`coverage_feedback_tuning` 等安全 action 会被物化为
  sandbox overlay/config artifact 和 make 变量，candidate evaluation 额外输出 action
  metrics、multi-candidate ranking 和 review-only promotion package。
- Harness LLM Optimization 第五阶段已接通 safe action runtime consumption：
  candidate regression 会注入 `HARNESS_RUNTIME_METRICS_OUT`；replay、scoreboard 和
  coverage feedback 业务层真实消费对应 sandbox config，并将执行计数汇总到
  `candidate_runtime_metrics` / candidate metrics。
- pyUVM replay 仍在 cocotb/pyUVM 生命周期内执行，但 replay context、sequence、
  driver/ref-model、scoreboard 和 coverage 的 connector 创建已统一迁移到
  `ReplayPipelineOrchestrator`；pyUVM component 只负责 phase 内调用行为，adapter 负责
  stage 包装。

## 后续迁移计划

迁移目标是将“调度编排逻辑”和“具体业务逻辑”分离，让后续可以在编排层中插入新的
agent、oracle、monitor、coverage advisor、trace collector、fault injector 或评测模块，
而不需要改动 DUT driver、pyUVM component 或 coverage/feedback 的核心业务实现。

### Connector 能力边界

当前 `Connector` 适合承担可观测调用边，而不是完整调度器：

- 适合：包装同步/异步调用，导出 started/finished/failed 事件，记录 metrics、
  metadata、artifact refs 和错误状态。
- 适合：在未启用 observer 时走低开销快速路径；启用后用 JSONL/monitor/topology
  记录组件健康和 artifact lineage。
- 适合：把外部命令、Python handler 和 pyUVM/cocotb 协程统一成同一套事件 schema。
- 不适合：直接承担 DAG 依赖解析、动态分支、重试策略、资源池、并发调度、缓存命中
  或多轮 campaign state 管理。
- 不适合：以 per-cycle/per-signal 粒度观测 RTL 仿真；默认粒度应保持在
  run、round、stage、case、transaction、coverage export 等边界。

因此后续迁移应继续把 connector 保持为“边”，把调度能力放在
`PipelineOrchestrator`、专用 orchestrator facade 和 topology/manifest 层。

### 分层目标

建议固定以下边界：

- `connector_observe/`：只定义事件、artifact reference、observer 和 connector wrapper。
- `fuzz_pipeline/orchestrator.py`：通用编排内核，负责 `StepSpec`、`PipelineContext`、
  role contract 校验、timeout、失败策略和 topology 导出。
- `fuzz_pipeline/stages/`：新增目录，放可复用 stage handler，例如 corpus generation、
  validation、UVM replay process、coverage report、coverage summary 和 feedback planning。
- `fuzz_pipeline/run_orchestrator.py`：顶层 run 编排，只组合 step，不直接写业务逻辑。
- `fuzz_pipeline/campaign_orchestrator.py`：campaign plan、manifest 和 connector 包装；
  mode/round 状态流转由 `CampaignRoundScheduler` 承接。
- `fuzz_pipeline/replay_orchestrator.py`：pyUVM replay 内部边界编排，只暴露 context、
  sequence、driver、ref model、scoreboard 和 coverage 的 step facade。
- `fuzz_bfm/`、`fuzz_uvm/`、`fuzz_feedback/`：保留业务实现，不直接创建 observer，
  不读取 connector 输出路径，不知道自己处在哪个 campaign。
- Makefile 和 CLI：逐步退化为薄入口，只解析环境/参数并调用 pipeline runner。

### 业务 handler contract

每个业务能力应拆成可被编排层调用的 handler。handler 可以读写 artifact，但不直接创建
connector，也不决定 topology 边：

- `generate_corpus_handler(config) -> CompletedProcess | CorpusResult`
- `validate_corpus_handler(config) -> list[FuzzCase]`
- `run_uvm_replay_handler(config) -> ReplayProcessResult`
- `run_verilator_coverage_handler(config) -> CoverageRunResult`
- `build_coverage_report_handler(config) -> CoverageReportResult`
- `build_coverage_summary_handler(config) -> dict`
- `plan_feedback_handler(config) -> FeedbackResult`
- `evaluate_round_handler(config) -> RoundEvaluation`

对应的 orchestrator 只负责把这些 handler 包装成 `StepSpec`，声明 connector 名称、
输入输出 artifact role、metrics、metadata、失败策略和 timeout。

### Stage Contract

run/campaign profile 层使用 `RunStage` 描述 stage contract，并用
`RunPlanProfile.stage_names` 决定实际 DAG：

- `requires_results` / `produces_results`：声明进程内 stage result 依赖和产出 key。
- `input_roles` / `output_roles`：声明跨 step artifact role 的读取和产出。
- `policy`：声明基础执行策略，例如 timeout、fail-fast 或 fail-open。
- `RunPlanProfile.mode`：约束 profile 只能用于对应 run mode，例如 `no_feedback`
  profile 不能被 `feedback_fuzz()` 入口误用。
- `RunPlanProfile.stage_policies`：按 stage name 覆盖 `RunStage.policy`，常用于把
  probe、sanitizer 或 evaluation 设为 fail-open。

执行前校验分为两层：

- profile/registry 层拒绝 unknown stage、factory stage name 不一致、mode 不匹配和
  policy 引用不存在的 stage。
- `RunPlan.validate()` 根据 stage 顺序拒绝缺失 result 依赖、缺失 artifact role、重复
  produced result key 和覆盖已有 result key。run plan 的初始 artifact role 来自
  `PipelineContext.artifacts`；campaign plan 的初始 result key 包含 `mode_runs`。

执行时 `RunPlanExecutor` 会再次检查 runtime result readiness，并校验声明的
`produces_results` 是否实际写入 `RunResults`。默认 policy 是 fail-fast；当
`StepPolicy(fail_main_on_step_error=False)` 时，stage 失败会记录到 `stage_errors` 并继续
执行后续不依赖该 result 的 stage。`StepPolicy.timeout_s` 会在单 stage handler 外层提供
同步 timeout。

默认 run profile：

- `generate_and_validate`：`corpus_generation -> corpus_validation`
- `coverage_run`：生成/校验 corpus 后执行 coverage replay
- `coverage_report`：在 coverage replay 后生成 Verilator coverage report
- `feedback_fuzz`：完整 feedback-guided 单轮
- `feedback_fuzz_with_evaluation`：完整单轮后追加 `round_evaluation`
- `no_feedback`：baseline 单轮，不生成 feedback corpus 和 feedback replay
- `no_feedback_with_evaluation`：baseline 单轮后追加 `round_evaluation`

默认 campaign profile：

- `campaign_manifest`：只聚合并写出 `campaign_manifest`
- `campaign_with_evaluation`：写出 manifest 后追加 `campaign_evaluation`
- `campaign_with_evaluation_and_optimization`：追加 phase-one harness optimization
  task/proposal/decision，默认 no-op optimizer 只生成可验证占位 proposal，不应用变更
- `campaign_with_evaluation_and_optimization_validation`：在 phase-one 后追加 sandbox
  apply、candidate evaluation、metric delta 和 final decision；默认候选验证为 `not_run`
  占位，可由 backend 替换；显式注入 `HarnessCandidateRegressionBackend` 后会运行 sandbox
  candidate campaign，并输出 candidate action overlay、adapter config、variant ranking
  和 promotion package

扩展点：

- run 层用 `register_run_stage()` / `register_run_plan_profile()` 插入自定义 stage 或
  profile。
- campaign 层用 `register_campaign_stage()` / `register_campaign_plan_profile()` 插入
  campaign-level stage。
- 执行 backend 用 `RunBackends(corpus_generator=..., uvm_replay=..., coverage_report=...)`
  替换外部命令实现。
- 评测 backend 用 `EvaluationBackends(round_evaluation=..., campaign_evaluation=...)`
  替换默认 JSON report 生成逻辑；`EvaluationBackends(harness_optimizer=...)` 可替换
  phase-one harness optimizer proposal backend；
  `EvaluationBackends(harness_candidate_evaluation=...)` 可替换第二阶段候选验证 backend。
  第三阶段可传入 `HarnessCandidateRegressionBackend(settings=...)`，让 candidate
  evaluation stage 运行真实 sandbox regression；第四阶段可通过
  `HarnessCandidateRegressionBackend(action_adapters=...)` 注入新的 safe action adapter，
  或覆盖默认 `replay_probe` / `scoreboard_check` / `coverage_feedback_tuning` 物化逻辑。

### 优先迁移点

1. 顶层 Makefile 流程迁移。
   `coverage-run`、`coverage-report`、`coverage-feedback` 和 `feedback-fuzz` 已迁入
   `FuzzRunOrchestrator`；`feedback-fuzz` 现在会在同一编排链路中生成单轮
   `round_manifest.json`；`feedback-campaign` 已将多轮 mode/round 状态机迁入
   `CampaignOrchestrator` 并生成 `campaign_manifest.json`。`sim` 现在只负责 replay
   已存在的 corpus；corpus generation/validation 由 orchestrator 显式调度。Makefile
   只保留 `$(PYTHON) scripts/run_fuzz_pipeline.py <subcommand>` 入口。

2. 扩展 `scripts/run_fuzz_pipeline.py`。
   已在 `generate-corpus` 之后增加 `coverage-run`、`coverage-report` 和
   `coverage-feedback`；`feedback-fuzz` 已把 feedback corpus、summary、directives、
   prompt、state artifact 的路径、LLM 选项和 previous-round 输入收敛到同一个
   `FuzzRunConfig`，并增加 `--round-manifest-out`、`--mode`、`--round-id`。
   `feedback-campaign` 已接入 `CampaignConfig`，负责 `--modes`、`--rounds`、上一轮
   `round_manifest` 状态读取和 campaign manifest。`feedback-fuzz` 可用
   `--run-plan-profile` / `--evaluation-out` 选择 round DAG 和 round evaluation；
   `feedback-campaign` 可用 `--run-plan-profile`、`--campaign-plan-profile`、
   `--round-evaluation` 和 `--campaign-evaluation-out` 选择 run/campaign DAG 与评测输出。
   CLI 只解析参数，实际顺序由 orchestrator 决定。

3. 引入 run/round manifest。
   单轮 `feedback-fuzz` 已生成 `round_manifest.json`，记录 target、mode、round、seed、
   输入 directives、corpus、RTL sources、coverage `.dat/.info`、functional coverage、
   summary、feedback state、connector event/monitor/topology 路径和命令 return code。
   `round_manifest.artifacts` 只把已物化的可选输出登记为可读状态；例如首轮没有
   previous summary 时不会登记未生成的 `gap_feedback` / `mutation_feedback`。
   多轮 `feedback-campaign` 已聚合每轮 manifest 为 `campaign_manifest.json`；启用
   `campaign_evaluation` profile 或 `--campaign-evaluation-out` 时会生成
   `evaluation_report.json`。

4. 废弃旧 `feedback_chain_*` 评测入口。
   `feedback_chain_compare.py`、`feedback_chain_code_only.py` 和
   `coverage_feedback_compare.py` 不再作为主 UVM-fuzz 迁移目标。新的多轮运行使用
   `run_fuzz_pipeline.py feedback-campaign`，新的评测/报告从 `campaign_manifest.json`
   或已接入的 `evaluation_report.json` 派生。

5. 拆分 pyUVM adapter。
   已拆成 `ReplayPluginBundle` 和 `ReplayStageAdapter`：前者负责 build driver/ref
   model/scoreboard/coverage，后者负责把 build/reset/execute/predict/check/sample/export
   包装成 orchestrator step。这样插入新的 oracle、monitor、trace collector 时不需要改
   driver 业务代码。

6. 保持 cocotb/pyUVM scheduler 边界。
   connector 不直接驱动 cocotb timing，也不替换 pyUVM phase。复杂初始化、burst、
   stateful flow 和 monitor-driven checking 应通过 sequence/monitor plugin contract
   扩展，再由 `ReplayPipelineOrchestrator` 暴露 `phase_to_sequence`、
   `monitor_to_record`、`record_to_scoreboard` 等 connector。

### 已接入的 run/campaign connector

保留现有 harness/feedback connector，同时接入流程级组件和边：

```text
corpus -> uvm_replay_process
target_manifest -> uvm_replay_process
rtl_sources -> uvm_replay_process
uvm_replay_process -> rtl_coverage_dat
uvm_replay_process -> replay_artifacts
rtl_coverage_dat -> coverage_report
coverage_report -> coverage_artifacts
coverage_artifacts -> coverage_summary
mutation_directives -> feedback_replay
round_artifacts -> round_manifest
round_manifest -> campaign_manifest
round_manifest -> round_evaluation
campaign_manifest -> evaluation_report
```

当前 connector 名称：

- `corpus_to_uvm_replay_process`
- `manifest_to_uvm_replay_process`
- `rtl_sources_to_uvm_replay_process`
- `uvm_replay_to_rtl_coverage`
- `uvm_replay_to_replay_artifacts`
- `rtl_coverage_to_coverage_report`
- `coverage_report_to_artifacts`
- `directives_to_feedback_replay`
- `round_artifacts_to_round_manifest`
- `round_manifest_to_campaign_manifest`
- `round_artifacts_to_evaluation`
- `campaign_to_evaluation_report`

这些 connector 优先包住外部命令和文件产物，不进入 DUT 专用协议细节。DUT protocol、
timing、reference model 和 scoreboard policy 仍由 plugin 或 pyUVM component 实现。

### 评测指标

迁移后的评测应从 connector events、monitor、round manifest 和 coverage summary 派生：

- 编排健康：connector started/finished/failed、return code、duration、timeout、
  required artifact 是否存在、topology contract 覆盖率。
- replay 质量：case count、case validation failure、driver exception、scoreboard
  checked/failures、ref model expected availability、functional coverage percent。
- feedback 效果：coverage delta、resolved/open/stale gap count、directive count、
  directive source、LLM real/fallback、每条 directive 的下一轮收益。
- 决策质量：计划是否引用真实 gap evidence，是否生成可验证 stimulus，是否减少重复无效
  尝试，是否触发 waiver/unreachable 标注。

### 迁移约束

- 新 schema 和 connector additive 增加，避免破坏已有 `coverage_feedback.py` 和旧
  summary 消费者；`feedback_chain_*` 只保留历史复现用途，不作为主流程兼容目标。
- plugin、pyUVM component 和 feedback 业务模块不直接创建 observer，也不直接读取
  `CONNECTOR_OBSERVE_OUT` / `CONNECTOR_MONITOR_OUT`。
- `PipelineContext.artifacts` 是跨 step 文件契约；`PipelineContext.values` 只保存
  当前进程内结果，不作为跨进程状态来源。
- run/round/stage/case 必须进入 metadata：`run_id`、`round_id`、`stage_id`、
  `target`、`mode`、`seed`、`case index`。
- observation 默认 fail-open；CI 或复现实验可设置 `STRICT_OBSERVATION=1`。

## Round Manifest

`feedback-fuzz` 默认通过 `round_artifacts_to_round_manifest` connector 写出
`coverage/<target>_round_manifest.json`。该文件是后续 campaign/evaluation 的稳定输入，
避免多轮脚本继续从目录名和默认文件名反推本轮状态。

manifest 顶层字段包括：

- `target`、`mode`、`round_id`、`run_id` 和 `cwd`。
- `config`：seed、iters、max seeds、LLM 选项、DUT top、RTL sources、命令 wrapper、
  `run_plan_profile` 和 `evaluation_out`。
- `artifacts`：corpus、feedback corpus、coverage summary、coverage `.dat/.info`、
  coverage replay functional coverage、feedback replay functional coverage、
  heuristic/final directives、prompt、feedback state、observation/monitor/topology
  和 manifest 自身路径。feedback 产物只在对应 stage 实际物化后登记；观测路径属于
  run lineage，即使 monitor summary 在 orchestrator 返回后 close 写出也会登记。
- `stages`：coverage replay、coverage report、coverage feedback 和 feedback replay 的
  return code 与命令。
- `coverage` 与 `feedback`：从 summary/directives 抽取的评测快照。
- 启用 round evaluation 时，`round_artifacts_to_evaluation` 会读取 `round_manifest`
  并写出 `evaluation_report`，该路径也会进入 artifacts。

## Campaign Manifest

`feedback-campaign` 通过 `CampaignOrchestrator` 串起多轮 `feedback-fuzz`，并在最后用
`round_manifest_to_campaign_manifest` connector 写出 `campaign_manifest.json`。campaign
层只处理调度状态，不进入 coverage、LLM 或 pyUVM 业务逻辑。

当前支持的 mode：

- `no_feedback`：每轮使用新的 seed 生成 corpus，只运行 corpus validation、coverage
  replay/report 和 round manifest，不引用上一轮 directives/state，也不生成 feedback
  corpus 或 feedback replay。
- `heuristic_feedback`：第 N 轮使用第 N-1 轮 directives，并把上一轮 summary、
  gap feedback 和 mutation feedback 传给 feedback planner。
- `llm_feedback`：接线方式同 heuristic，但启用 LLM 路径；无可用 LLM 时仍保留
  heuristic fallback，除非显式要求 real LLM。

`campaign_manifest.json` 记录：

- campaign 配置：target、modes、rounds、seed、iters、LLM 选项、RTL sources、
  `run_plan_profile`、`campaign_plan_profile` 和 round/campaign evaluation 开关。
- 每个 mode 下每轮 `round_manifest` 路径、上一轮 `round_manifest`、应用的上一轮
  directives/state、coverage snapshot、feedback snapshot 和 stage return code。
- campaign 级 observation、monitor、topology、manifest 路径；启用 campaign evaluation
  时还包含 `evaluation_report` 路径。

`CampaignOrchestrator` 在 feedback mode 中把上一轮 `round_manifest` 转换为
`RoundManifestState`，再从 manifest 的 `artifacts` 字段读取 canonical
`coverage_summary`、`mutation_directives`、`gap_feedback` 和 `mutation_feedback` 路径。
因此调整 round 目录命名不会影响下一轮 previous-state 接线。

round/campaign evaluation 的默认 JSON report 是轻量评测快照：round report 汇总本轮
stage 名称、return code、case count、coverage/feedback snapshot 和 manifest artifact；
campaign report 汇总 mode/round 数、每轮 coverage/feedback snapshot 和
`campaign_manifest` lineage。若本轮配置了 `observation_events`，默认 evaluation 会调用
`HarnessTraceBuilder` 生成 harness execution records、harness evaluation 和 LLM
optimization dataset，并把这些产物路径与摘要挂到 `harness_trace` 字段。需要替换为更复杂
的 Agentic Harness Engineering 评测时，仍应通过 `EvaluationBackends` 注入 backend，而不是
让 run/campaign orchestrator 直接实现评测业务。

## Harness Trace 聚合

Harness Trace 聚合被拆成多层：`connector_observe.trace` 负责通用 event/quality；
`harness_records.py` 负责 execution record 投影；`harness_metadata.py` 负责可替换的
case/directive/corpus 归因；`harness_analysis.py` 负责 UVM-fuzz 业务评测；
`harness_llm_tasks.py` 负责面向 LLM 的数据视图；`harness_rollup.py` 负责 campaign
跨轮聚合。`HarnessTraceBuilder` 仍作为兼容 facade，把 connector event stream 与
round/campaign manifest 聚合为以下派生产物：

- `<evaluation>_harness_records.jsonl`：逐 connector 终态执行记录，每条记录包含
  run/round/stage/case、`span_id`、`case_id`、`directive_id`、`corpus_sha256`、
  target/mode、connector、from/to layer、step、duration、status、metrics、metadata、
  error 和 evidence path。
- `<evaluation>_harness_evaluation.json`：按 connector、module、case、directive、
  failure cluster 和 slowest record 聚合的评测报告，同时保留 coverage/feedback snapshot、
  manifest artifacts 和 `trace_quality`。
- `<evaluation>_llm_dataset.jsonl`：面向 LLM 自动优化 harness 的样本，优先保留失败和慢
  record，并用 artifact path 引用大文件证据。
- `<campaign_evaluation>_campaign_rollup.json`：仅 campaign evaluation 生成，按 round、
  coverage metric、case、directive 和 failure trend 聚合跨轮表现。

聚合器只读取已存在的 JSONL/JSON artifact，不重新执行 DUT、coverage 或 feedback 逻辑。
默认 evaluation 在读取事件前会 flush 当前 observer；如果事件文件不存在或聚合失败，主
evaluation report 仍保持可写，`harness_trace` 只作为 additive 诊断信息。

如果需要接入其它业务分析，例如 Agentic Harness Engineering 的 task 生成、跨 round
趋势分析或外部评测服务，应注入新的 metadata extractor、record projector、
`HarnessAnalyzer` 或 LLM dataset builder，或通过 `EvaluationBackends` 替换默认
evaluation backend；通用 trace core 不承载业务判断。

每次 connector invocation 都会生成一个 top-level `span_id`，同一次调用的
`connector.started` 与 `connector.finished` / `connector.failed` 共享该值。聚合器通过
`span_id` 检测 hanging span、orphan final event、重复 final event 和缺失 span 的旧事件；
默认 round/campaign evaluation 在自身 connector 尚未 finished 时读取事件，因此会把当前
`round_artifacts_to_evaluation` / `campaign_to_evaluation_report` 的开放 span 记入
`ignored_hanging_spans`，避免污染 harness hanging 统计。

pyUVM replay 的 case 级事件会补充稳定 `case_id`、`case_sha256`、`directive_id` 和
`corpus_sha256`。`case_id` 优先使用显式 `case_id` / `testcase_id`；否则由 stimulus payload
hash 派生，并排除 `origin`、`directive_id` 等观测/归因字段，避免同一 stimulus 因不同
directive 来源被误认为不同 case。`directive_id` 优先来自 case 显式字段，缺省时使用 corpus
generator 写入的 `origin`，以便把 directive 与后续 replay/scoreboard/coverage 行为关联起来。

## 环境变量

启用观测通常只需要设置以下变量：

```sh
CONNECTOR_OBSERVE_OUT=coverage/connector_events.jsonl
CONNECTOR_MONITOR_OUT=coverage/component_monitor.json
CONNECTOR_TOPOLOGY_OUT=coverage/component_topology.json
CONNECTOR_OBSERVE_RUN_ID=my_run
```

可选变量：

- `CONNECTOR_OBSERVE_ASYNC=0`：关闭异步 observer，测试时常用。
- `CONNECTOR_OBSERVE_QUEUE=1024`：异步队列长度。
- `CONNECTOR_OBSERVE_ROUND_ID`、`CONNECTOR_OBSERVE_STAGE_ID`、
  `CONNECTOR_OBSERVE_PARENT_EVENT_ID`：在 connector metadata 中标记 fuzz round、
  stage 和父事件。
- `OBSERVATION_RUN_ID`：兼容通用 observation context；如果同时设置
  `CONNECTOR_OBSERVE_RUN_ID`，后者优先。
- `OBSERVATION_ROUND_ID`、`OBSERVATION_STAGE_ID`、`OBSERVATION_PARENT_EVENT_ID`：
  通用 observation context 的兼容变量；对应 `CONNECTOR_OBSERVE_*` 变量优先。
- `STRICT_OBSERVATION=1`：observer 失败时让主链路失败。默认关闭。

输出 artifact：

- `CONNECTOR_OBSERVE_OUT`：JSONL 事件，每行一个 connector event。
- `CONNECTOR_MONITOR_OUT`：组件健康汇总 JSON。多进程流程会合并已有 snapshot。
- `CONNECTOR_TOPOLOGY_OUT`：完整 component/connector topology JSON。
- 启用 round/campaign evaluation 且存在 observation event 文件时，还会写出
  `*_harness_records.jsonl`、`*_harness_evaluation.json` 和 `*_llm_dataset.jsonl`。

## 已接入的 harness 连接

当前 `HARNESS_TOPOLOGY` 覆盖以下边：

```text
target_manifest -> corpus_generator
mutation_directives -> corpus_generator
corpus_generator -> corpus
corpus -> corpus_validator
corpus -> replay_context
target_manifest -> replay_driver
target_manifest -> ref_model
replay_context -> sequencer
sequencer -> replay_driver
replay_driver -> dut
replay_driver -> ref_model
replay_driver -> scoreboard
replay_driver -> functional_coverage
scoreboard -> scoreboard_report
functional_coverage -> functional_coverage_summary
```

对应 connector 名称包括：

- `manifest_to_corpus_generator`
- `directives_to_corpus_generator`
- `corpus_generator_to_corpus`
- `corpus_to_validation`
- `corpus_to_replay_context`
- `manifest_to_replay_driver`
- `manifest_to_ref_model`
- `replay_context_to_sequence`
- `case_to_replay_driver`
- `driver_reset_to_dut`
- `case_to_dut`
- `case_to_ref_model`
- `driver_to_scoreboard`
- `driver_to_functional_coverage`
- `scoreboard_to_report`
- `functional_coverage_to_summary`

这些连接覆盖了从生成 stimulus 到 replay 检查和 functional coverage 导出的主要
harness 层级。实际事件数量取决于当前入口和配置；例如无 directives 的
`generate-corpus` 不会触发 `directives_to_corpus_generator`。

## 已接入的 feedback 连接

coverage feedback 使用同一套 connector：

```text
coverage_artifacts -> coverage_summary
coverage_summary -> mutation_feedback
mutation_feedback -> gap_feedback
gap_feedback -> layer1_plan
layer1_plan -> heuristic_directives
layer1_plan -> mutation_directives
mutation_directives -> llm_prompt
llm_prompt -> llm_response
llm_response -> mutation_directives
```

关键 connector：

- `coverage_to_summary`
- `summary_to_mutation_feedback`
- `layer3_feedback_to_layer2_feedback`
- `layer2_layer3_feedback_to_layer1_plan`
- `layer1_plan_to_heuristic_directives`
- `layer1_plan_to_directives`
- `summary_to_llm_prompt`
- `llm_prompt_to_response`
- `llm_response_to_directives`

这里把三层反馈间的影响显式化：Layer 3 direction feedback 进入 Layer 2 per-gap
feedback，Layer 2/3 共同影响 Layer 1 plan，最后 Layer 1 plan 再生成下一轮
directives。

## Makefile 使用示例

生成 corpus 并导出观测信息：

```sh
uv run make -C libafl_bfm_fuzz \
  TARGET=secworks_sha256 \
  LIBAFL_ITERS=0 \
  LIBAFL_MAX_SEEDS=0 \
  COVERAGE_DIR=libafl_bfm_fuzz/coverage/observe_smoke \
  CONNECTOR_OBSERVE_OUT=libafl_bfm_fuzz/coverage/observe_smoke/events.jsonl \
  CONNECTOR_MONITOR_OUT=libafl_bfm_fuzz/coverage/observe_smoke/monitor.json \
  CONNECTOR_TOPOLOGY_OUT=libafl_bfm_fuzz/coverage/observe_smoke/topology.json \
  CONNECTOR_OBSERVE_RUN_ID=observe_smoke \
  generate-corpus
```

需要指定 cargo wrapper 时，把 `CARGO` 作为一个 quoted Makefile 变量传入：

```sh
uv run make -C libafl_bfm_fuzz \
  TARGET=secworks_sha256 \
  CARGO='cargo +nightly' \
  generate-corpus
```

完整 feedback fuzz：

```sh
uv run make -C libafl_bfm_fuzz \
  TARGET=secworks_sha256 \
  VERILOG_SOURCES="/path/to/rtl/*.v" \
  TOPLEVEL=sha256 \
  CONNECTOR_OBSERVE_OUT=coverage/connector_events.jsonl \
  CONNECTOR_MONITOR_OUT=coverage/component_monitor.json \
  CONNECTOR_TOPOLOGY_OUT=coverage/component_topology.json \
  CONNECTOR_OBSERVE_RUN_ID=sha256_feedback \
  ROUND_ID=round_00 \
  feedback-fuzz
```

启用 round evaluation：

```sh
uv run make -C libafl_bfm_fuzz \
  TARGET=secworks_sha256 \
  VERILOG_SOURCES="/path/to/rtl/*.v" \
  TOPLEVEL=sha256 \
  ROUND_EVALUATION_ENABLE=1 \
  feedback-fuzz
```

选择 campaign profile 并启用 campaign evaluation：

```sh
uv run make -C libafl_bfm_fuzz \
  TARGET=secworks_sha256 \
  VERILOG_SOURCES="/path/to/rtl/*.v" \
  TOPLEVEL=sha256 \
  CAMPAIGN_MODES=no_feedback,heuristic_feedback \
  CAMPAIGN_ROUNDS=2 \
  ROUND_EVALUATION_ENABLE=1 \
  CAMPAIGN_EVALUATION_ENABLE=1 \
  feedback-campaign
```

显式启用 phase-one harness optimization：

```sh
uv run make -C libafl_bfm_fuzz \
  TARGET=secworks_sha256 \
  VERILOG_SOURCES="/path/to/rtl/*.v" \
  TOPLEVEL=sha256 \
  CAMPAIGN_PLAN_PROFILE=campaign_with_evaluation_and_optimization \
  CAMPAIGN_EVALUATION_ENABLE=1 \
  feedback-campaign
```

显式启用 sandbox candidate validation：

```sh
uv run make -C libafl_bfm_fuzz \
  TARGET=secworks_sha256 \
  VERILOG_SOURCES="/path/to/rtl/*.v" \
  TOPLEVEL=sha256 \
  CAMPAIGN_PLAN_PROFILE=campaign_with_evaluation_and_optimization_validation \
  CAMPAIGN_EVALUATION_ENABLE=1 \
  feedback-campaign
```

该 profile 会额外写出 `harness_optimization_patch`、
`harness_optimization_candidate_manifest`、`harness_optimization_candidate_evaluation`、
`harness_optimization_metric_delta` 和 `harness_optimization_final_decision`。默认候选验证
不会运行真实回归，因此 proposed action 会停在 review/validation 框架内；接入真实
candidate backend 后，final decision 才可能进入 `accepted_for_review`。

真实 candidate regression backend 需要从 Python 侧显式注入：

```python
from fuzz_pipeline import (
    CandidateAcceptanceThresholds,
    CandidateRegressionSettings,
    EvaluationBackends,
    HarnessCandidateRegressionBackend,
)

evaluation_backends = EvaluationBackends(
    harness_candidate_evaluation=HarnessCandidateRegressionBackend(
        settings=CandidateRegressionSettings(
            modes=("heuristic_feedback",),
            rounds=1,
            thresholds=CandidateAcceptanceThresholds(
                max_regressed_metric_count=0,
                min_improved_metric_count=1,
            ),
        ),
    ),
)
```

## 事件与 monitor 示例

JSONL event 中常用字段：

```json
{
  "schema_version": 1,
  "event_type": "connector.finished",
  "connector": "case_to_dut",
  "from_layer": "replay_driver",
  "to_layer": "dut",
  "run_id": "sha256_feedback",
  "span_id": "3edfd6c1-7fd3-41c3-9e8a-52e3d46a41bb",
  "duration_ms": 0.123,
  "inputs": [],
  "outputs": [],
  "metrics": {
    "has_expected": true,
    "matched": true,
    "detail": "..."
  },
  "metadata": {
    "target": "secworks_sha256",
    "run_id": "sha256_feedback",
    "round_id": "round_01",
    "stage_id": "replay",
    "index": 0,
    "origin": "libafl_seed",
    "case_id": "b37d0e2b8f3e9a1c",
    "case_sha256": "...",
    "directive_id": "libafl_seed",
    "corpus_sha256": "..."
  },
  "status": "ok"
}
```

monitor JSON 聚合：

```json
{
  "schema_version": 1,
  "event_count": 4,
  "connector_count": 2,
  "failed_connector_count": 0,
  "connectors": [
    {
      "connector": "corpus_generator_to_corpus",
      "started": 1,
      "finished": 1,
      "failed": 0,
      "metrics": {
        "returncode": 0
      }
    }
  ]
}
```

## 性能与隔离

- 未配置 `CONNECTOR_OBSERVE_OUT` 和 `CONNECTOR_MONITOR_OUT` 时，
  `observer_from_env()` 返回 `NullObserver`，connector 直接执行被包装函数。
- observer 异常默认被隔离，不影响主链路结果。
- pyUVM replay 中的 driver/ref-model/scoreboard/coverage 观测由
  `ReplayPipelineOrchestrator` 使用 `run_step_async()` 或轻量同步 step 包装；metrics 只取
  已有 summary/result，不重新执行 DUT 行为。
- 多进程 Makefile 流程中，`MonitoringObserver` 会读取并合并已有 monitor snapshot，
  避免后一个进程覆盖前一个进程的汇总。
- `HarnessTraceBuilder` 是离线聚合层，只把终态事件写成 execution record，同时读取
  started/final span 配对信息生成 `trace_quality`；大文件继续以 artifact path 形式进入
  evidence，不内联到事件或 LLM dataset。
- `corpus_sha256` 只在启用 observation 时计算，并按 path/size/mtime 缓存，避免无观测默认
  replay 路径额外扫描大型 corpus。
- `PipelineOrchestrator` 的同步 timeout 会返回 `TimeoutError`；底层线程如果无法被
  Python 强制停止，可能仍短暂运行，因此 handler 应尽量保持幂等。

## 测试覆盖

当前测试覆盖：

- connector 同步/异步执行和失败事件。
- observer failure isolation 和 strict mode。
- `PipelineOrchestrator` step 执行、role contract 校验、async step 防误用和 timeout。
- `ReplayPipelineOrchestrator` 对 driver/ref-model/scoreboard/coverage step 的委托执行。
- monitor 聚合与跨进程 snapshot 合并。
- coverage feedback 事件、monitor 和 topology 导出。
- 三层 feedback connector 导出。
- `RunPlan` 静态校验 result/artifact contract、重复 result key、runtime result
  readiness、fail-open policy 和 timeout。
- run profile/stage registry 可插入自定义 stage、拒绝 mode 不匹配和未知 policy stage。
- `EvaluationBackends` 可替换 round/campaign evaluation backend。
- `EvaluationBackends(harness_optimizer=...)` 可替换 phase-one harness optimizer backend，
  生成 proposal 并由 decision artifact 做 schema accept/reject。
- `EvaluationBackends(harness_candidate_evaluation=...)` 可替换第二阶段 candidate
  validation backend；`HarnessCandidateRegressionBackend` 会物化 sandbox run config，
  复用 campaign/run 编排执行候选回归，并生成 baseline/candidate metric delta、阈值化
  final decision、adapter metrics、runtime action metrics、candidate variant ranking 和
  promotion package。
- campaign profile 可插入自定义 campaign stage，并能拒绝缺失 `campaign_manifest`
  依赖的非法 DAG。
- `feedback-fuzz` 单轮 manifest 写出和 `round_artifacts_to_round_manifest` 观测。
- `feedback-campaign` 多轮 manifest 聚合和 `round_manifest_to_campaign_manifest` 观测。
- `CampaignRoundScheduler` 能从上一轮 manifest 携带 directives/summary/state 到下一轮。
- `HarnessTraceBuilder` 能把 connector events、monitor、round manifest 聚合为执行记录、
  harness evaluation 和 LLM optimization dataset，覆盖 span_id 配对、hanging span 检测、
  case/directive 聚合、trace quality report，以及可注入 analyzer/dataset builder；默认
  round evaluation 在发现事件文件时会自动附加 `harness_trace`。
- harness command wrapper。
- replay context 加载观测。
- Makefile `generate-corpus` smoke 可导出 corpus generator 和 validator 事件。
