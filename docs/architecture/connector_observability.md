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

`py/fuzz_pipeline/harness.py`

- 为 Makefile 命令包装和 observation context 提供通用 helper。
- 负责从环境变量创建 observation context、导出拓扑、包装外部命令。

`scripts/run_connector.py`

- 命令包装入口，用于观测 Rust LibAFL corpus generator 等外部进程。

`py/fuzz_uvm/observable.py`

- 提供 `ObservableReplayDriverAdapter`、`ObservableScoreboardAdapter` 和
  `ObservableCoverageAdapter`。
- pyUVM component 通过 adapter 调用 driver/ref-model/scoreboard/coverage；adapter 再委托
  `ReplayPipelineOrchestrator` 执行观测 step，不直接创建 connector。

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
- `scripts/run_connector.py` 还会校验 CLI 传入的 `--from-layer` / `--to-layer` 与
  topology 中的 connector endpoint 一致。
- observer 仍默认 fail-open；只有 `STRICT_OBSERVATION=1` 或 step policy 关闭
  `fail_open_observation` 时，观测失败才会影响主链路。

当前迁移状态：

- corpus generation、corpus validation 和 coverage feedback 已由 `PipelineOrchestrator`
  编排。
- pyUVM replay 仍在 cocotb/pyUVM 生命周期内执行，但 replay context、sequence、
  driver/ref-model、scoreboard 和 coverage 的 connector 创建已统一迁移到
  `ReplayPipelineOrchestrator`；pyUVM component 与 adapter 只负责调用行为。

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
  feedback-fuzz
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
    "origin": "libafl_seed"
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
- harness command wrapper。
- replay context 加载观测。
- Makefile `generate-corpus` smoke 可导出 corpus generator 和 validator 事件。
