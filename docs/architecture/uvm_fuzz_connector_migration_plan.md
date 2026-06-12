# UVM-Fuzz Connector Migration Plan

本文档记录主 UVM-fuzz 流程迁移到 connector 编排框架的执行计划。目标是让
`FuzzRunOrchestrator` / `CampaignOrchestrator` 拥有调度顺序、round state 和 artifact
接线；DUT driver、pyUVM component、coverage/feedback 模块只保留各自业务逻辑。

## 当前边界

主流程入口：

- `make generate-corpus`
- `make coverage-run`
- `make coverage-report`
- `make coverage-feedback`
- `make feedback-fuzz`
- `make feedback-campaign`
- `scripts/run_fuzz_pipeline.py <subcommand>`

废弃边界：

- `scripts/feedback_chain_compare.py` 和 `scripts/feedback_chain_code_only.py` 不再作为主流程
  迁移目标；后续只保留历史复现实验用途，新的多轮评测应读取
  `campaign_manifest.json`。
- `scripts/coverage_feedback_compare.py` 只作为旧 smoke/对照脚本，不承担主 UVM-fuzz
  编排职责。
- `scripts/run_connector.py` 只作为通用命令观测工具，不作为 UVM-fuzz 主入口。

## 当前迁移状态

- `FuzzRunOrchestrator` 已负责 corpus generation、validation、coverage replay、
  Verilator coverage report、coverage feedback、feedback replay 和 round manifest 写出。
  第一阶段 `RunPlan` / `RunStage` 已落地，`feedback_fuzz` 与 `no_feedback` 不再直接
  在方法体里手写完整顺序，而是由有序 stage plan 执行，后续可以在 plan 中插入新模块。
  第二阶段已把 command/path/replay/report 业务执行细节迁到 `run_adapters.py`：
  `RunPathResolver` 管 artifact 与 cwd 解析，`CorpusGeneratorAdapter` 管 cargo corpus
  命令，`UvmReplayAdapter` 管 make replay/观测变量，`CoverageReportAdapter` 管
  Verilator coverage report。`FuzzRunOrchestrator` 保留 connector step 包装和 plan
  组装，旧方法名只作为委托兼容层。
  第三阶段已引入 `RunStageRegistry`：默认 run plan 由 stage name 列表装配，外部可以
  注册自定义 stage 并覆盖 plan stage 顺序，用于后续插入 backend probe、artifact
  sanitizer、round evaluation 等模块，而不再改写默认 DAG builder。
  第四阶段已完成调度框架升级：`RunPlanProfile` 描述 run/campaign DAG profile，
  `RunBackends` 允许替换 corpus/replay/coverage backend adapter，`round_evaluation`
  与 `campaign_evaluation` 作为 registry stage 接入；`EvaluationBackends` 允许替换
  round/campaign evaluation adapter；默认 profile 仍保持现有 `feedback_fuzz` /
  `no_feedback` 行为不变。
  第五阶段已完成 Stage Contract 升级：`RunStage` 声明 `requires_results`、
  `produces_results`、`input_roles`、`output_roles` 和 `policy`；`RunPlan` 在执行前
  静态校验 result/artifact 依赖、重复 result key 和 profile policy 引用，执行时再检查
  runtime result readiness。
  第六阶段已完成 Harness LLM Optimization 第一阶段：新增 campaign profile
  `campaign_with_evaluation_and_optimization`，在 `campaign_evaluation` 后生成
  `harness_optimization_task`、`harness_optimization_proposal` 和
  `harness_optimization_decision`。默认 no-op optimizer backend 只生成 schema-valid
  proposal，占位 decision 做 schema-level accept/reject，并将应用状态标为
  `not_applied`。
  第七阶段已完成 Harness LLM Optimization 第二阶段框架：新增 campaign profile
  `campaign_with_evaluation_and_optimization_validation`，在第一阶段后追加 sandbox
  apply、candidate evaluation、metric delta 和 final decision；安全 action 子集只转换为
  sandbox candidate artifacts，默认不修改源码主线。
  第八阶段已完成 Harness LLM Optimization 第三阶段：新增
  `HarnessCandidateRegressionBackend`，可将安全 proposal action 子集物化为 sandbox run
  配置，并复用 `CampaignOrchestrator` / `FuzzRunOrchestrator` 执行 candidate campaign；
  final decision 支持基于 candidate status、回归指标和最小改善数的阈值化判断。
  第九阶段已完成 Harness LLM Optimization 第四阶段：新增 candidate action adapter 层，
  将 `replay_probe`、`scoreboard_check`、`coverage_feedback_tuning` 等安全 action 转换为
  sandbox overlay/config artifact 和 `HARNESS_*_CONFIG` make 变量；candidate evaluation
  增加 adapter metrics、multi-candidate ranking 和 review-only promotion package。
  第十阶段已完成 Harness LLM Optimization 第五阶段：新增 safe action runtime
  consumption，pyUVM replay/scoreboard 与 coverage feedback 业务层会在显式 candidate
  regression 中消费 sandbox config，并通过 `HARNESS_RUNTIME_METRICS_OUT` 导出 runtime
  action metrics。
  第十一阶段已完成 Harness LLM Optimization 第六阶段：candidate regression 会把 runtime
  metrics 按 `action_id` / `action_type` 聚合为 `candidate_action_effect_report`，并可通过
  `CandidateRegressionSettings.max_variant_regressions` / `attribution_top_k` 启用 top-K
  variant 独立 sandbox 回归，或通过 `attribution_mode=all_actions` 评测所有 action
  variants；默认值保持只执行 combined candidate 的现有行为。
  第十二阶段已完成 Harness LLM Optimization 第七阶段：新增真实 LLM optimizer backend
  接入。`LlmHarnessOptimizerBackend` 会基于 harness evaluation、LLM dataset、
  campaign rollup 和 action effect report 构造 optimizer prompt artifact，调用
  OpenAI-compatible chat transport，解析并校验 schema-valid proposal；若 proposal 不满足
  safe action DSL/schema，会把校验错误加入 repair prompt 并按配置 retry。最终 proposal
  会携带 prompt/response artifact、model、attempt count 和 repair 状态等 LLM provenance。
  默认 campaign profile 和 no-op optimizer 行为不变，不会自动修改源码主线。
- `CampaignOrchestrator` 已负责 campaign-level plan、manifest 和 connector 包装；
  mode/round 展开、上一轮 `round_manifest` 状态读取和每轮 `FuzzRunConfig` 构造已抽到
  `CampaignRoundScheduler`。
- `CoverageFeedbackPipeline` 已把 coverage summary、Layer 2/3 feedback、Layer 1 plan、
  heuristic directives、LLM response/final directives 放在 connector step 中，并由各
  step 在完成前物化自身 artifact；heuristic 草案和最终 mutation directives 使用不同
  文件，避免 LLM/fallback 覆盖中间 lineage。
- `ReplayPipelineOrchestrator` 已在 pyUVM 内部包装 replay context、sequence、driver、
  ref model、scoreboard 和 functional coverage 边界；`ReplayPluginBundle` 与
  `ReplayStageAdapter` 已拆分插件构建和 connector stage 包装。

## 调度框架设计基线

当前主 UVM-fuzz 编排框架由四层组成：

- profile 层：`RunPlanProfile` 用 `stage_names` 描述 run/campaign DAG；`mode` 限制
  profile 可用于的 run mode；`stage_policies` 允许按 stage 覆盖 timeout、fail-fast 或
  fail-open 策略。
- registry 层：`RunStageRegistry` 负责把 stage name 构造成 `RunStage`，并在执行前拒绝
  unknown stage、重复注册和 factory 返回错误 name。
- contract 层：`RunStage` 声明 `requires_results`、`produces_results`、`input_roles`、
  `output_roles` 和 `policy`。`RunPlan.validate()` 在执行前静态检查 result/artifact
  依赖、重复 result key 和覆盖已有 result key。
- executor 层：`RunPlanExecutor` 在每个 stage 运行前检查 runtime result readiness，在
  stage 返回后检查声明的 `produces_results` 是否实际写入；fail-open stage 的异常进入
  `stage_errors`，后续不依赖失败 result 的 stage 可继续运行。

默认 run profile：

- `generate_and_validate`
- `coverage_run`
- `coverage_report`
- `feedback_fuzz`
- `feedback_fuzz_with_evaluation`
- `no_feedback`
- `no_feedback_with_evaluation`

默认 campaign profile：

- `campaign_manifest`
- `campaign_with_evaluation`
- `campaign_with_evaluation_and_optimization`
- `campaign_with_evaluation_and_llm_advice`
- `campaign_with_evaluation_and_optimization_validation`
- `campaign_with_evaluation_and_optimization_real_validation`

公开扩展点：

- `FuzzRunOrchestrator.register_run_stage()` / `register_run_plan_profile()`：插入或替换
  run-level DAG stage。
- `CampaignOrchestrator.register_campaign_stage()` /
  `register_campaign_plan_profile()`：插入 campaign-level stage。
- `RunBackends`：替换 corpus generator、UVM replay、coverage report 后端。
- `EvaluationBackends`：替换 round/campaign evaluation 后端。
- `EvaluationBackends(harness_optimizer=...)`：替换 phase-one harness optimizer proposal
  backend；默认 `NoopHarnessOptimizerBackend` 不应用变更。需要真实 LLM 生成 proposal
  时可注入 `LlmHarnessOptimizerBackend`，它会写出 optimizer prompt/response provenance
  artifact，并在失败时执行 schema repair/retry；需要先审查 prompt 或把 prompt 交给外部
  LLM 时可注入 `PromptOnlyHarnessOptimizerBackend`，它写出 prompt 和 `not_called`
  response provenance，但不调用 LLM transport。
- `campaign_with_evaluation_and_llm_advice`：advice-only profile，在
  task/proposal/decision 后追加 `harness_optimization_advice_report`，输出 observations、
  recommended actions、handoff 和 reproducibility，并显式声明未触发 sandbox apply、
  candidate regression 或源码主线修改。Make 入口为 `llm-harness-advice-campaign` 和
  `prompt-only-harness-advice-campaign`。
- `EvaluationBackends(harness_candidate_evaluation=...)`：替换第二阶段 candidate
  validation backend；默认 `NoopHarnessCandidateEvaluationBackend` 生成 `not_run`
  validation report，不运行真实回归。
- `HarnessCandidateRegressionBackend`：第三阶段真实 candidate validation backend；从
  baseline campaign manifest 派生 sandbox candidate campaign 配置，写出
  `candidate_regression_config` / `candidate_action_overlay` / 可选
  `candidate_mutation_directives`，并通过 `campaign_with_evaluation` profile 跑候选回归。
  CLI/Make 可用 `--harness-candidate-backend real` 或
  `real-candidate-feedback-campaign` 启用；`--harness-optimizer-backend llm` 可同时接入
  现有 LLM optimizer backend 生成非 no-op proposal。真实 CLI 路径默认先运行 matched
  no-op baseline，并用同配置空动作 rerun metrics 作为 candidate metric delta 的
  baseline；可用 `--no-candidate-matched-baseline` 或
  `HARNESS_CANDIDATE_MATCHED_BASELINE=0` 关闭。
  第九阶段新增 paired repeated validation：真实 CLI/Make 默认 `candidate-paired-repeats=3`，
  matched no-op 与 candidate 使用同一 seed 序列成对运行，输出
  `candidate_paired_validation`，并将 mean/worst/variance/flaky 统计接入 final decision。
  第四阶段进一步公开 `CandidateActionAdapter` / `JsonConfigActionAdapter`，默认把
  `replay_probe`、`scoreboard_check`、`coverage_feedback_tuning` 等 safe action 物化为
  per-action config artifact，并产出 `candidate_variant_ranking` 和
  `candidate_promotion_package`。第五阶段新增 `harness_evidence/runtime_actions.py`，让这些
  per-action config 在 candidate run 内被 replay/scoreboard/coverage feedback 真实消费，
  并把 `candidate_runtime_metrics` 合入 candidate evaluation metrics。第六阶段新增
  `candidate_variant_evaluations` 和 `candidate_action_effect_report`，用于记录 top-K 或
  all-actions variant 独立回归结果、runtime action consumption 状态和每个 action 的效果归因；
  action effect report 会把 action 分类为 `improved`、`neutral`、`regressed` 或
  `not_consumed`，promotion package 会引用该汇总。若 single-action variant 已评测，
  汇总 `effect_status` 采用 standalone 结果，并额外写出 `combined_effect_status` /
  `aggregate_effect_status`，用于区分独立有效 action 和只出现在有效组合中的辅助 action。
  第十阶段新增 promotion action pruning：promotion package 基于 standalone attribution
  输出 `effective_actions`、`neutral_actions`、`harmful_actions`、
  `recommended_promotion_actions`、`minimal_promotion_candidate` 和
  `action_pruning_summary`。最小候选只保留独立有效 action；若精确 action set 已有
  `passed`/`ok` variant 证据则可进入 `ready_for_review`，否则需要重新验证；无有效
  action 的候选会留下空 minimal candidate 并停在 `not_recommended`。第十一阶段新增
  actionability-driven optimization：safe action DSL 增加 `mmio_readback`，replay runtime
  可通过 target driver 的 symbolic MMIO resolver 读取寄存器；candidate regression 输出
  `candidate_gap_actionability_report`，把剩余 RTL gap 分类为 readback 可触达、需要 MMIO
  write surface、需要 internal-state surface/waiver 或 unknown，并把 summary 接入
  promotion package。`secworks_aes` 的真实验证在 matched no-op + `paired_repeats=3` +
  all-actions attribution 下稳定将 `uncovered_line_count` 从 18 降到 13，且 promotion
  package 能把 neutral directive 剪掉，只保留有效的 `mmio_readback` minimal candidate。
- 第十二阶段新增 harness optimization plugin registry：`HarnessPluginRegistry` 把 safe
  action DSL、payload validator、adapter config、runtime attribution 标记和 gap
  actionability classifier 从核心评测流程中解耦。Python API 可通过
  `HarnessOptimizationAdapter(plugin_registry=...)`、
  `HarnessCandidateRegressionBackend(plugin_registry=...)` 或
  `EvaluationBackends(harness_plugin_registry=...)` 注入；CLI/Make 可通过
  `--harness-optimization-plugin` / `HARNESS_OPTIMIZATION_PLUGINS` 加载 `module:Object`
  插件，target manifest 可通过 `[harness_optimization].plugins` 声明目标专用评测插件。
  默认 registry 只保留通用 built-in actions，不再叠加任何 DUT 专属 classifier。
- 第十三阶段把 `secworks_aes` actionability classifier 从核心迁到
  `fuzz_examples.secworks_aes_harness_plugin`，并通过 `secworks_aes.toml` 的
  `[harness_optimization].plugins` 显式加载；同时 registry snapshot 增加
  `plugin_validation`、`plugin_provenance` 和 snapshot schema，candidate action effect、
  gap actionability 与 promotion package 都能审查插件来源和契约状态。runtime manager
  增加通用 lifecycle hook，并保持既有 `sample_after_execute` replay/mmio 行为兼容。
- 第十四阶段将 plugin registry 推进为可门禁、可复现的迁移闭环：provenance 记录
  `source_file`、`source_sha256` 和 registry fingerprint；real candidate validation
  profile 默认开启 strict plugin validation gate，CLI/Make 可用
  `--strict-harness-plugin-validation` / `HARNESS_STRICT_PLUGIN_VALIDATION` 控制；
  replay/ref-model/scoreboard 路径实际触发 runtime lifecycle hook；candidate regression
  基于 gap actionability 额外生成 `candidate_gap_actionability_minimal_proposal`，用于把
  target plugin 给出的 recommendation 转成下一轮可验证候选。
- CLI/Makefile：`--run-plan-profile`、`--campaign-plan-profile`、`--round-evaluation`、
  `--evaluation-out`、`--campaign-evaluation-out` 以及对应 Makefile 变量
  `RUN_PLAN_PROFILE`、`CAMPAIGN_PLAN_PROFILE`、`ROUND_EVALUATION_ENABLE`、
  `CAMPAIGN_EVALUATION_ENABLE`；harness optimization 插件入口为
  `--harness-optimization-plugin` 与 `HARNESS_OPTIMIZATION_PLUGINS`，strict plugin gate
  入口为 `--strict-harness-plugin-validation` 与 `HARNESS_STRICT_PLUGIN_VALIDATION`。

## 迁移任务

1. 冻结主入口与废弃边界。
   文档只把 `run_fuzz_pipeline.py` 和 Makefile 主目标作为推荐入口；旧评测脚本标记为
   deprecated。

2. 拆除 Makefile 隐式调度。
   `sim` 只运行 pyUVM/cocotb replay，不再隐式依赖 `generate-corpus`；需要 replay smoke
   时由 `check-uvm` 或文档命令显式先生成 corpus。

3. 显式化 `feedback-fuzz` DAG。
   `feedback-fuzz` 的顺序固定为：
   `generate_corpus -> validate_corpus -> coverage_replay -> coverage_report ->
   coverage_feedback -> generate_feedback_corpus -> validate_feedback_corpus ->
   feedback_replay -> round_manifest`。
   `no_feedback` campaign round 使用较短 DAG：
   `generate_corpus -> validate_corpus -> coverage_replay -> coverage_report ->
   round_manifest`，不生成 feedback corpus、summary、directives 或 feedback replay。
   已完成第一阶段：这些 DAG 现在通过 `RunPlan` stage 列表表达，stage handler 仍复用
   `FuzzRunOrchestrator` 现有 connector-wrapped 方法。
   已完成第二阶段：外部命令和路径派生由 run adapters 承接，orchestrator 不再直接拼装
   cargo/make/verilator 命令。
   已完成第三阶段：默认 DAG 现在通过 `RunStageRegistry` 和 `DEFAULT_RUN_PLAN_STAGE_NAMES`
   装配，`FuzzRunOrchestrator.register_run_stage()` 与
   `set_run_plan_stage_names()` 可用于插入新阶段。
   已完成第四阶段：`run_profiles.py` 提供 `feedback_fuzz`、`no_feedback` 及带
   evaluation 的 profile；`FuzzRunConfig.run_plan_profile` / `CampaignConfig.run_plan_profile`
   可选择每轮 DAG，`CampaignConfig.campaign_plan_profile` 可选择 campaign-level DAG。
   `round_evaluation` 读取 `round_manifest` 作为稳定输入，`campaign_evaluation` 读取
   `campaign_manifest`，两者均通过 connector step 写出 `evaluation_report`。campaign
   层可通过 `register_campaign_stage()` 和 `register_campaign_plan_profile()` 插入
   自定义 campaign stage。
   已完成第五阶段：每个默认 run/campaign stage 声明 result/artifact contract；
   `RunPlanProfile.stage_policies` 可按 stage 覆盖基础执行策略，例如把非关键 probe 或
   evaluation stage 设置成 fail-open。plan 构建会在执行前拒绝缺失上游 result、缺失
   artifact role、重复 result key、profile mode 不匹配、未知 stage 和未知 policy stage；
   执行时会再次拒绝上游 fail-open 后未实际产出的 runtime result。

4. 后端 adapter 可替换。
   已完成：`RunBackends` 可注入 `CorpusGeneratorBackend`、`ReplayBackend` 和
   `CoverageReportBackend`；默认实现仍为 `CorpusGeneratorAdapter`、`UvmReplayAdapter`
   和 `CoverageReportAdapter`，继续走当前 cargo/make/verilator 行为。测试可注入 fake
   backend，后续 remote backend 或 agentic harness backend 可复用同一接口。
   `EvaluationBackends` 可注入 round/campaign evaluation backend，让评测逻辑独立于
   run/campaign 编排。默认 evaluation 已接入 `HarnessTraceBuilder`，在存在 observation
   event 文件时自动生成 execution records、harness evaluation 和 LLM optimization dataset；
   execution records 包含 `span_id`、`case_id`、`directive_id` 和 `corpus_sha256`，
   harness evaluation 额外提供 hanging span、case/directive 聚合和 trace quality report；
   replacement backend 仍可完全接管这部分逻辑。
   Harness optimization 第一阶段已完成：`harness_evidence/optimization.py` 从 campaign
   evaluation、harness evaluation、LLM dataset 和 campaign rollup 构造结构化 task；
   optimizer backend 返回 proposal；decision artifact 只做 schema-level accept/reject，
   不执行 sandbox apply。advice-only profile 进一步写出 advice report，用于把 LLM 建议和
   后续 validation handoff 分离。
   Harness optimization 第二阶段框架已完成：显式 validation profile 将 accepted proposal
   转换成 sandbox-only candidate artifacts，生成 candidate manifest、candidate evaluation、
   baseline/candidate metric delta 和 final decision；`ref_model_patch` 等潜在源码修改 action
   先被标为 unsafe/skipped，默认 backend 不修改源码、不运行真实候选回归。
   Harness optimization 第三阶段已完成：`HarnessCandidateRegressionBackend` 会把
   `mutation_directive_update` 转换成 sandbox 初始 directives，把其它安全 action 物化到
   action overlay，然后复用 campaign/run 编排执行 candidate campaign；candidate evaluation
   输出 baseline/candidate metrics 和 acceptance thresholds，final decision 按
   gateable metric 的 `max_regressed_metric_count`、`min_improved_metric_count`
   和 accepted status 阈值判断。质量门禁指标包括 failure、trace health 和 coverage
   指标；`directive_count`、`case_count`、`record_count` 与 runtime/action 计数保留为
   informational comparison，不再因数量下降或上升单独触发 candidate rejection。
   Harness optimization 第四阶段已完成：safe action 物化由 action adapter 层负责，
   默认 adapter 支持 `replay_probe`、`scoreboard_check`、`coverage_feedback_tuning`、
   `stimulus_generation_hint` 和 `documentation_note` 的 sandbox config 输出；candidate
   evaluation 会统计 action/overlay/directive/variant metrics，生成多候选 ranking 和
   review-only promotion artifact，仍不修改源码主线。
   Harness optimization 第五阶段已完成：candidate campaign 通过 `extra_make_vars`
   注入 `HARNESS_REPLAY_PROBE_CONFIG`、`HARNESS_SCOREBOARD_CHECK_CONFIG`、
   `HARNESS_COVERAGE_FEEDBACK_TUNING_CONFIG` 和 `HARNESS_RUNTIME_METRICS_OUT`；
   runtime consumer 会写出 replay probe、scoreboard check 和 coverage feedback tuning
   的执行指标，candidate evaluation 会读取这些指标参与 metric delta。
   Harness optimization 第六阶段已完成：runtime consumer 会写出 per-action metrics；
   candidate regression 会生成 action effect report，并在显式设置
   `max_variant_regressions > 1` 时为 selected variants 分别物化 sandbox config、执行
   candidate campaign、生成 variant evaluation，再由 ranking/final decision 选择 top
   variant。默认主流程与默认 candidate regression 行为不变。
   Harness optimization 第七阶段已完成：真实 LLM optimizer backend 会把 campaign
   evaluation、harness evaluation、LLM dataset、campaign rollup 和
   `candidate_action_effect_report` 汇总成结构化 prompt；LLM response 必须转成
   schema-valid proposal，否则会通过 repair prompt 重试。safe action DSL 扩展到
   `scoreboard_check.field_equals/field_range`、`replay_probe` 采样约束和
   `coverage_feedback_tuning` 的权重 clamp/过滤字段；runtime consumer 与 candidate
   evaluation 保持只在显式 sandbox candidate 流程中消费这些配置。
   Harness optimization 第八阶段已完成：新增
   `campaign_with_evaluation_and_optimization_real_validation` profile、CLI/Make 参数和
   `real-candidate-feedback-campaign` target，可从命令行选择真实 candidate regression
   backend，并通过 `HARNESS_CANDIDATE_*` / `CANDIDATE_*` 参数覆盖候选 rounds、modes、
   seed、matched no-op baseline、paired repeats、repeat seed stride、top-K/all-actions
   attribution、max flaky metrics 和 acceptance thresholds。`real-candidate-feedback-campaign`
   现在走 strict evidence 默认：非零 candidate fuzz budget、至少一个 gateable improvement、
   零 gateable regression、零 flaky metric；`real-candidate-smoke-campaign` 保留零预算
   neutral-pass 快速闭环。

5. 收紧 artifact materialization。
   已完成：每个 coverage feedback connector step 声明的 output artifact 会在 step
   finished 前落盘；`round_manifest` 只登记已物化的可选输出，例如第一轮未生成
   `gap_feedback` / `mutation_feedback` 时不会把计划路径写入 artifacts。观测相关的
   observation/monitor/topology 路径是运行配置 lineage，即使 monitor summary 在
   orchestrator 返回后才 close 写出，也会被 manifest 记录。
   coverage replay 与 feedback replay 的 UVM functional coverage 使用不同 artifact：
   `functional_coverage` 作为 feedback summary 的输入，`feedback_functional_coverage`
   只描述最终 feedback replay。

6. 让 manifest 成为状态源。
   已完成：campaign 下一轮从上一轮 `round_manifest` 的 `artifacts` 字段读取 canonical
   `coverage_summary`、`mutation_directives`、`gap_feedback` 和 `mutation_feedback` 路径，
   不再依赖目录命名规则推导 previous state。

7. 拆分 pyUVM adapter。
   已完成：`ReplayPluginBundle` 负责 build driver/ref model/scoreboard/coverage；
   `ReplayStageAdapter` 负责把 build/reset/execute/predict/check/sample/export 包装成
   connector step。pyUVM component 不直接处理 connector env、round/campaign state 或
   functional coverage artifact 接线。

8. 验证与代码审查。
   当前 Python 环境由 `uv` 管理；每阶段至少运行 `uv run python -m py_compile`、
   Makefile dry-run、`uv run make -C libafl_bfm_fuzz check`；涉及 campaign 的变更需要跑
   小规模 `feedback-campaign` dry-run 并检查 manifest/monitor/topology。

## 当前测试映射

- `tests/test_run_plan.py`：覆盖 ordered execution、mapping merge、initial results、
  静态 result/artifact contract 校验、重复 result key、runtime readiness、declared output
  校验、fail-open policy 和 timeout 行为。
- `tests/test_run_stage_registry.py`：覆盖 registry 插入自定义 run stage、profile policy
  让插入 stage fail-open，以及 unknown/factory name mismatch 等 registry 约束。
- `tests/test_run_adapters.py`：覆盖 path/cwd 解析、cargo/make/verilator command adapter
  和 backend 替换边界。
- `tests/test_run_profiles_evaluation.py`：覆盖 run/campaign profile 选择、mode mismatch
  拒绝、round/campaign evaluation stage、`EvaluationBackends` 替换、自定义 campaign
  stage、非法 campaign DAG 拒绝、harness optimization campaign profile 和 fake optimizer
  backend、LLM advice-only profile 不触发 candidate backend、harness optimization validation
  profile 和 fake candidate validation backend、real validation profile 以及 CLI real/prompt-only
  backend factory，外加
  `CampaignRoundScheduler` 对上一轮 manifest state 的传递。
- `tests/test_harness_optimization.py`：覆盖 phase-one harness optimization task/proposal/
  decision/advice report artifact 写出、默认 no-op proposal、prompt-only prompt provenance、
  非法 proposal schema reject、sandbox apply、unsafe action skip、candidate metric delta、
  final decision、真实 candidate regression
  backend 的 sandbox run config 物化、safe action adapter config 输出、内部 campaign
  编排调用、adapter metrics、runtime action schema/消费/指标聚合、action effect report、
  top-K/all-actions variant 独立回归、multi-candidate ranking、promotion package、阈值 reject、真实
  LLM optimizer prompt/response provenance、proposal repair/retry，以及扩展 safe action
  DSL 的 schema reject。
- `tests/test_harness_trace.py`：覆盖 connector events 到 harness execution records 的转换、
  connector/module/failure/case/directive 聚合、hanging span 检测、trace quality report、
  LLM optimization dataset 写出、通用 trace core 独立使用、可注入 metadata extractor、
  analyzer/dataset builder、campaign trace rollup 写出，以及默认 round evaluation 在发现
  observation events 时自动附加 `harness_trace`。
- `tests/test_fuzz_observe_connector.py` / `tests/test_connector_observe.py`：覆盖 connector
  started/final 事件共享同一 `span_id`，以及 pyUVM replay 事件携带 `case_id`、
  `directive_id` 和 `corpus_sha256`。

## 完成标准

- 主 UVM-fuzz 流程不依赖旧 `feedback_chain_*` 脚本。
- Makefile 不再隐藏 corpus generation；调度顺序在 orchestrator 中可读、可观测。
- round/campaign manifest 能解释每轮输入、输出、previous state 和 connector lineage。
- DUT/pyUVM/feedback 业务模块不直接承担编排状态和 artifact 接线。
- 文档、dry-run、编译和现有测试均更新并通过。
