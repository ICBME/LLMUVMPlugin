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
- `libafl_bfm_fuzz`：提供 corpus generation、JSONL validation、pyUVM replay、
  scoreboard/ref-model hook、functional coverage 和 coverage feedback。

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

## 模块边界

框架核心负责：

- 读取 manifest。
- 生成和校验 JSONL corpus。
- 加载插件。
- 调度 pyUVM replay。
- 收集结构覆盖和 schema 层 functional coverage。
- 生成 generic mutation directives。

目标插件负责：

- DUT reset 和事务时序。
- case 到协议 transaction 的转换。
- reference model 或 oracle。
- 目标语义 scoreboard。
- 目标 functional coverage。

## 关键设计点

- Manifest 描述 case schema 和插件位置。
- IR 描述语义信号与 HDL path 的绑定。
- Rust generator 只理解 manifest field schema，不理解 DUT 语义。
- pyUVM replay 只理解 `reset()` / `execute(case)` driver 协议。
- coverage feedback 只能产生符合 schema 的 directives。

## 非目标

- 不在框架核心内置任何 DUT 或示例工程。
- 不在框架核心实现 memory-mapped、streaming、AXI、APB 等协议。
- 不在 IR resolver 中执行 cocotb timing 操作。
- 不让 LLM 输出绕过 schema validation。
