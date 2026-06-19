# Overall Architecture

本文档给出 `rtlagent_bfm` 与 `libafl_bfm_fuzz` 的总体关系。

## 分层视图

```text
source bundle / RTL / docs / registers
              |
              v
       SemanticSpecIR / agent-generated IR
              |
              v
   LLM candidate plugin artifacts
              |
              v
  validation + final artifact promotion
              |
              v
    rtlagent_bfm runtime layer
              |
              v
 target driver / ref model / scoreboard plugins
              |
              v
       libafl_bfm_fuzz replay framework
              |
              v
        DUT simulation + coverage
```

系统分为几类主要模块：

- `rtlagent_bfm`：提供 IR、HDL path resolver 和生成 BFM 的运行时访问层。
- `Spec2Backend/Spec2IR`：提供自然语言 spec 到 `SemanticSpecIR` 的可溯源语义抽取、
  审查和 repair loop。
- `Spec2Backend/FeedbackCodegen`：提供 RefModelPlan 到 reference model candidate 的
  反馈闭环 LLM 代码生成、验证和 final artifact promotion。
- `Spec2Backend/RefModelDSL`：提供 RefModelPlan 到可验证 RefModelIR 的反馈闭环生成、
  DSL 解释执行、Z3 verification、外部调用策略和 deterministic UVM wrapper 生成。
- `LLMPlugin`：提供 Spec2IR 和后续生成链路共享的插件化 LLM backend。
- `libafl_bfm_fuzz`：提供 corpus generation、JSONL validation、pyUVM replay、
  scoreboard/ref-model hook、functional coverage 和 coverage feedback；仓库内的
  `fuzz_examples` 与 `targets/secworks_*` 是 smoke/example target，不是核心依赖。

## 数据流

```text
target manifest
      |
      +--> Rust LibAFL generator --> JSONL corpus
      |
      +--> Python ReplayContext ----+
                                    |
JSONL corpus ----------------------> pyUVM sequence
                                    |
                                    v
                              target driver
                                    |
                                    v
                                  DUT RTL
                                    |
             +----------------------+----------------------+
             v                                             v
      ref model / scoreboard                         coverage subscriber
             |                                             |
             v                                             v
      pass/fail summary                         UVM functional coverage
                                                           |
                                                           v
                                                 coverage feedback CLI
                                                           |
                                                           v
                                                   mutation directives
```

框架同时维护一张 connector topology，用于观测上图中关键组件连接：

```text
corpus_generator -> corpus -> replay_context -> sequencer -> replay_driver
replay_driver -> dut / ref_model / scoreboard / functional_coverage
coverage_artifacts -> coverage_summary -> Layer 2/3 feedback -> Layer 1 plan
```

connector 事件、monitor 汇总和 topology JSON 的格式见
[Connector Observability 架构](connector_observability.md)。

## 模块边界

框架核心负责：

- 读取 manifest。
- 生成和校验 JSONL corpus。
- 加载插件。
- 调度 pyUVM replay。
- 收集结构覆盖和 schema 层 functional coverage。
- 将结构覆盖导出为 structured coverage export，并聚合为 `rtl_gap`。
- 生成 generic mutation directives。

实现分层建议：

- `ConnectGraph`：通用 connector event、observer、topology、trace quality 和拓扑查询。
  package root 只暴露这些图事实/观测/拓扑能力；历史 orchestration import 仅通过
  `ConnectGraph.orchestrator` 兼容 facade 保留。
- `harness_optimization`：通用 run planning、path helper、optimization protocol、
  plugin/runtime helper 内核，以及通用 orchestration / planning、candidate regression
  共用的 execution model、candidate validation kernel、protocol/rules kernel、trace core
  和 campaign rollup kernel。
  其中 `candidate_execution.py` 负责候选执行模型、action loading / adapter dispatch、
  JSON config adapter 与 adapter 结果，
  `candidate_validation.py` 负责 metric snapshot、not-run/error report、action effect
  attribution、gap actionability/minimal candidate 提炼，以及 repeat/variant/promotion
  等候选验证公共计算，
  `runtime.py` 负责共享 env/path/runtime metrics helper，以及 runtime action config/entry
  loader、runtime plugin spec 装配和 lifecycle hook manager，
  `action_dsl.py` 负责共享 builtin action DSL schema、payload validator 和 builtin
  plugin factory 单一来源，
  `rules.py` 负责 proposal schema、payload DSL、metric gate 和 final decision 规则，
  `optimization.py` 负责共享 task/prompt/advice builder、optimizer transport、
  proposal backend 和 runtime adapter，
  `planning.py` 负责共享 `RunPlan` / `RunStage` / `RunPlanExecutor`、stage registry /
  profile 以及 stage wrapper、profile policy 应用 helper，
  `topology.py` 负责共享 topology facade，稳定暴露 graph component / connector 类型、
  merge/write helper 和 topology env helper，
  `orchestrator.py` 负责共享 `StepSpec` / `PipelineContext` / `PipelineOrchestrator`，
  `libafl_bfm_fuzz` 内部业务模块直接复用该共享编排内核，
  `observation.py` 负责共享 observation/runtime facade，收敛
  `ObservationContext`、observer lifecycle、make vars 和 topology env helper，
  `campaign_optimization.py` 负责 campaign 级 harness optimization stage chain、
  manifest artifact helper 和业务 adapter 注入边界，
  `evaluation.py` 负责 round/campaign evaluation payload、trace attachment 和 campaign
  rollup attachment 的共享编排内核，
  `records.py` 负责通用 harness execution record 投影内核，
  `analysis.py` 负责通用 harness evaluation 聚合内核，
  `trace.py` 负责通用 harness trace build/write 协议，
  `rollup.py` 负责 campaign 跨轮聚合内核。
- `fuzz_pipeline`：`libafl_bfm_fuzz` 业务编排和 facade；新业务代码优先依赖
  `harness_evidence.*`、`harness_plugins`、`harness_runtime_actions`，而不是兼容 wrapper。

目标插件负责：

- DUT reset 和事务时序。
- case 到协议 transaction 的转换。
- reference model 或 oracle。
- 目标语义 scoreboard。
- 目标 functional coverage。

## 关键设计点

- Manifest 描述 case schema 和插件位置。
- IR 描述语义信号与 HDL path 的绑定。
- Spec2IR 的 `SemanticSpecIR` 描述自然语言规格的可溯源语义，是 ref model、SVA 或
  其他 backend artifact planning 之前的可信审查层；它不记录 backend support 判断。
- LLM 生成的 ref model / scoreboard 必须先通过 candidate validation，再作为
  final plugin 由 manifest 接入；ref model 的可信推荐路径是先生成 RefModelIR/DSL，
  再由框架生成 wrapper。
- RefModelIR/DSL 对可形式化规则使用 Z3 验证；标准参考实现通过
  `trusted_standard` provenance 和 conformance 接入，不宣称 SMT 证明标准库本身。
- OracleIR 生成的 ref model 也复用同一条 candidate/final validation 链路；当前
  VerilogEval smoke 测试覆盖 stateless 小规模组合逻辑，详见
  [Reference Model OracleIR 评估](ref_model_oracle_ir_eval.md)。
- Rust generator 只理解 manifest field schema，不理解 DUT 语义。
- pyUVM replay 只理解 `reset()` / `execute(case)` driver 协议。
- coverage feedback 只能产生符合 schema 的 directives。
- advisor 只消费 coverage summary、`rtl_gap` 和 functional gap，不直接解析工具原始
  coverage artifact。
- connector observer 只包裹组件边界；默认隔离 observer 失败，不改变 corpus、replay、
  scoreboard 或 coverage feedback 的主结果。

## 非目标

- 不让框架核心依赖任何 DUT 或示例工程；示例 target 只能通过普通 manifest/plugin 接入。
- 不在框架核心实现 memory-mapped、streaming、AXI、APB 等协议。
- 不在 IR resolver 中执行 cocotb timing 操作。
- 不让 LLM 输出绕过 schema validation。
