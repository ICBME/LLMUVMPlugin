# Design Constraints

本文档列出后续实现必须遵守的约束。

## 核心边界

- 框架核心不得出现特定 DUT、示例工程或协议实现。
- 新目标必须通过 manifest 和 plugin 接入。
- Manifest schema 是 Rust generator 和 Python validator/replay 的共享契约。
- IR 只描述语义绑定，不描述协议行为。

## IR 约束

- `protocol` 是标签，不是行为开关。
- 生成 BFM 可以依赖语义名，不应硬编码易变 HDL path。
- `hdl_path` 解析只做 lookup，不做信号发现或自动修复。
- `registers` 只表达布局，不表达 bus transaction。
- timing、reset、handshake、monitor 和 scoreboard 行为属于生成代码或插件。

## Fuzz/Replay 约束

- 默认 replay 粒度是一个 JSONL case 对应一次 `execute(case)`。
- 默认 scoreboard 要求 expected。
- 默认 coverage 只表达 schema 层覆盖。
- `Makefile` 不拥有 DUT RTL 路径；调用方必须传入 `VERILOG_SOURCES` 和 `TOPLEVEL`。
- 所有生成文件、coverage、sim build、crash corpus 都应留在忽略目录中。

## LLM 约束

- 覆盖反馈路径中，LLM 只能生成 mutation directives。
- ref model / scoreboard 生成路径中，LLM 只能生成 candidate artifact，不得直接更新
  final artifact 或 replay core。
- LLM 输出必须经过对应验证：directive validation、schema decode、静态检查、插件契约
  检查和必要的 golden case 检查。
- LLM 不应绕过 target manifest；最终接入仍通过 `bfm_ir`、`ref_model`、`scoreboard`
  等 manifest 字段完成。
- Prompt 中可以包含 coverage summary、IR、manifest 和 spec，但不应要求框架核心理解
  目标协议。

## 插件约束

- Driver plugin 是协议行为边界。
- Ref model 不驱动 DUT。
- Scoreboard 负责目标 pass/fail 策略。
- Coverage model 负责目标语义覆盖。
- Plugin constructor 应显式声明需要的参数，减少静默忽略错误。

## 推荐扩展顺序

1. Manifest validation：统一 Rust/Python 错误报告。
2. Scoreboard policy：提供 smoke scoreboard 或 `allow_missing_expected`。
3. Sequence plugin：支持多阶段初始化、burst 和 stateful replay。
4. Mutator schema：支持字段权重、边界值、交叉约束和依赖关系。
5. Coverage schema：用 manifest 声明通用 coverpoint/cross。
6. DSL codegen：在直接生成 Python 难以稳定时，将 ref model / scoreboard 语义收敛到
   可验证 DSL。
