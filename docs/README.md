# RTLAgent BFM Documentation

本文档集描述清理后的 BFM 生成、fuzz、pyUVM replay 和 coverage feedback
框架。文档按层次拆分，先读总览，再按需要进入架构、参考规范和接入指南。

## 推荐阅读顺序

1. [总体架构](architecture/overview.md)
2. [IR 与运行时架构](architecture/ir_runtime.md)
3. [LLM Plugin Codegen 架构](architecture/llm_plugin_codegen.md)
4. [Reference Model OracleIR 评估](architecture/ref_model_oracle_ir_eval.md)
5. [Fuzz/Replay 架构](architecture/fuzz_replay.md)
6. [Connector Observability 架构](architecture/connector_observability.md)
7. [UVM-Fuzz Connector 迁移计划](architecture/uvm_fuzz_connector_migration_plan.md)
8. [Coverage Feedback 设计](architecture/coverage_feedback_design.md)
9. [Coverage Feedback 评估](architecture/coverage_feedback_eval.md)
10. [Target Manifest 参考](reference/target_manifest.md)
11. [插件契约](reference/plugin_contracts.md)
12. [Corpus 与 Mutation Directives](reference/corpus_directives.md)
13. [接入新 DUT 指南](guides/add_new_dut.md)
14. [Secworks 示例闭环](guides/secworks_examples.md)
15. [设计约束](guides/design_constraints.md)

## 文档层次

`architecture/`

- 说明系统边界、数据流和模块职责。
- 面向框架维护者和新功能设计者。

`reference/`

- 说明 manifest、JSONL、plugin 和 directive 的稳定接口。
- 面向目标插件作者和自动生成器。

`guides/`

- 说明如何接入新 DUT，以及哪些约束必须保持。
- 面向实际使用者和后续实现者。

## 核心原则

- 框架核心保持 DUT 无关。
- DUT 专用协议、reference model、scoreboard 和 coverage model 均通过插件接入。
- Manifest 是 Rust corpus generator 和 Python replay/validation 之间的共享契约。
- IR 只描述语义名到 HDL path 的映射，不表达协议行为。
- Connector observability 只观察组件连接、artifact、metrics 和错误状态，不改变
  harness 主链路执行结果。
- LLM 生成的 ref model / scoreboard 和 OracleIR 生成的 ref model 都先作为 candidate
  artifact，经验证后才能提升为 manifest 指向的 final artifact。

## Import Guidance

- 通用 connector / observer / topology / orchestration：优先使用 `ConnectGraph.*`。
- 通用 planning / path / optimization protocol：优先使用 `harness_optimization.*`。
- `libafl_bfm_fuzz` 业务逻辑：优先使用 `fuzz_pipeline.harness_evidence.*` 以及
  `fuzz_pipeline.harness_plugins`、`fuzz_pipeline.harness_runtime_actions`。
- `fuzz_pipeline.harness*.py`、`fuzz_pipeline.run_plan`、`fuzz_pipeline.run_stage_registry`
  是兼容入口；新代码除非需要保留旧 import 路径，否则不应新增对这些模块的依赖。
