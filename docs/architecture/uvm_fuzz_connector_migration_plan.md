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

公开扩展点：

- `FuzzRunOrchestrator.register_run_stage()` / `register_run_plan_profile()`：插入或替换
  run-level DAG stage。
- `CampaignOrchestrator.register_campaign_stage()` /
  `register_campaign_plan_profile()`：插入 campaign-level stage。
- `RunBackends`：替换 corpus generator、UVM replay、coverage report 后端。
- `EvaluationBackends`：替换 round/campaign evaluation 后端。
- CLI/Makefile：`--run-plan-profile`、`--campaign-plan-profile`、`--round-evaluation`、
  `--evaluation-out`、`--campaign-evaluation-out` 以及对应 Makefile 变量
  `RUN_PLAN_PROFILE`、`CAMPAIGN_PLAN_PROFILE`、`ROUND_EVALUATION_ENABLE`、
  `CAMPAIGN_EVALUATION_ENABLE`。

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
   replacement backend 仍可完全接管这部分逻辑。

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
  stage、非法 campaign DAG 拒绝，以及 `CampaignRoundScheduler` 对上一轮 manifest state
  的传递。
- `tests/test_harness_trace.py`：覆盖 connector events 到 harness execution records 的转换、
  connector/module/failure/case 聚合、LLM optimization dataset 写出，以及默认 round
  evaluation 在发现 observation events 时自动附加 `harness_trace`。

## 完成标准

- 主 UVM-fuzz 流程不依赖旧 `feedback_chain_*` 脚本。
- Makefile 不再隐藏 corpus generation；调度顺序在 orchestrator 中可读、可观测。
- round/campaign manifest 能解释每轮输入、输出、previous state 和 connector lineage。
- DUT/pyUVM/feedback 业务模块不直接承担编排状态和 artifact 接线。
- 文档、dry-run、编译和现有测试均更新并通过。
