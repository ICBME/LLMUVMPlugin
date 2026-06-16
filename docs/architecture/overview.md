# Overall Architecture

本文档给出 `rtlagent_bfm` 与 `libafl_bfm_fuzz` 的总体关系。

## 分层视图

```text
source bundle / RTL / docs / registers
              |
              v
       agent-generated IR
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

系统分为两条主线：

- `rtlagent_bfm`：提供 IR、HDL path resolver 和生成 BFM 的运行时访问层。
- `rtlagent_bfm.codegen`：提供 LLM plugin candidate 写入、OracleIR ref model 生成、
  校验、提升和 manifest 接入工具。
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

- `ConnectGraph`：通用 connector event、observer、topology 和 step orchestration。
- `harness_optimization`：通用 run planning、path helper、optimization protocol、
  plugin/runtime helper 内核。
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
- LLM 生成的 ref model / scoreboard 必须先通过 candidate validation，再作为
  final plugin 由 manifest 接入。
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
