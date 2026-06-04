# Reference Model OracleIR Evaluation

本文档记录当前 reference model 生成、接入方式，以及使用
`example/verilog-eval` 对 OracleIR ref model 链路做 smoke/regression 测试的结果。

## 当前实现路径

当前 ref model 有两条生成路径：

- 直接插件路径：LLM 或人工生成 Python bundle，经静态检查、插件契约检查和可选
  golden case 检查后，提升为 final artifact。
- OracleIR 路径：先生成受限 DSL 形式的 `OracleIR`，再由
  `build_oracle_plugin_bundle()` 生成 `GeneratedOracleRefModel` Python 插件，之后复用
  同一套 candidate/final validation 流程。

运行时接入点保持一致：target manifest 的 `ref_model` 或 `oracle` 字段指向最终
Python 插件；pyUVM replay 通过 `build_ref_model()` 加载插件，并在 driver 得到
actual 后调用 `ref_model.predict(case)` 填充 expected，再交给 scoreboard 比对。

## OracleIR 表达范围

当前 `OracleIR` 适合表达 deterministic、stateless 的输入到输出映射：

- 小规模 truth table：每条 rule 用 `when` 条件匹配输入字段，用 literal expected
  表达输出。
- 少量内置调用：如 `hashlib.sha*`、`binascii.crc32`、`zlib.crc32`。
- 基本表达式：literal、field、concat、slice、lower_hex、bytes_from_hex、call。
- 基本条件：eq、and、or、not。

当前不适合表达：

- edge-triggered sequential logic。
- latch 或依赖历史状态的输出。
- 显式 `x/z` 语义。
- 输入状态空间过大的组合逻辑 truth table。
- 多周期 monitor-driven scoreboard 行为。

## VerilogEval 测试脚本

脚本位置：

```sh
libafl_bfm_fuzz/scripts/verilog_eval_oracle_chain_test.py
```

默认数据集：

```sh
example/verilog-eval/dataset_spec-to-rtl
```

脚本流程：

1. 解析每个 `*_ref.sv` 的 `RefModule` 端口。
2. 筛出当前 OracleIR truth-table 不适合覆盖的样例，包括无输出、输入位宽超过阈值、
   edge-triggered sequential logic、显式 `x/z` literal。
3. 用 Verilator 编译参考 `RefModule` 和自动生成的 `tb.sv`。
4. 枚举输入组合，生成 golden truth table。
5. 将 truth table 转成 OracleIR rules。
6. 生成 `GeneratedOracleRefModel` bundle。
7. 调用现有 `finalize_bundle()`，用 golden cases 验证 candidate 并提升为 final。
8. 输出 JSON/Markdown 报告。

常用命令：

```sh
uv run python libafl_bfm_fuzz/scripts/verilog_eval_oracle_chain_test.py \
  --limit -1 \
  --max-input-bits 8
```

指定样例：

```sh
uv run python libafl_bfm_fuzz/scripts/verilog_eval_oracle_chain_test.py \
  --problem Prob001_zero \
  --problem Prob005_notgate \
  --problem Prob009_popcount3 \
  --fail-on-failure \
  --fail-on-empty
```

输出目录：

```text
libafl_bfm_fuzz/coverage/verilog_eval_oracle_chain/
```

## Verilator 与 Icarus 选择

当前 OracleIR 链路测试默认使用 Verilator。原因是该测试只需要把 VerilogEval 的
`RefModule` 当作离线 golden generator：编译参考 RTL，枚举输入，打印 expected。
这与项目中 pyUVM/cocotb replay 默认偏向 Verilator 的环境一致。

Verilator 可以替代 Icarus 用于当前 OracleIR 链路测试，但不能视作官方
VerilogEval pass-rate harness 的等价替代：

- 官方 VerilogEval harness 的 Makefile 和日志分析脚本围绕 Icarus 输出设计。
- Verilator 的 warning/error 分类、latch 处理和 SystemVerilog 支持边界与 Icarus 不完全相同。
- Verilator 编译成本更高，适合离线 golden 生成，不适合每条 fuzz transaction 热路径。

因此建议：

- OracleIR/ref model 链路回归：使用 Verilator。
- 与 VerilogEval 官方结果对齐的 pass-rate 对比：保留 Icarus harness。

## 2026-06-04 测试结果

环境：

```text
Verilator 5.048
```

基础校验：

```sh
uv run python -m py_compile libafl_bfm_fuzz/scripts/verilog_eval_oracle_chain_test.py
uv run python -m pytest tests/test_codegen_pipeline.py -q
```

`tests/test_codegen_pipeline.py` 结果为 `15 passed`。

完整链路测试结果：

| Metric | Value |
| --- | ---: |
| Total VerilogEval problems | 156 |
| Screened in and attempted | 55 |
| Passed | 54 |
| Failed | 1 |
| Screened out | 101 |
| Passed golden cases | 2265 |

报告：

```text
libafl_bfm_fuzz/coverage/verilog_eval_oracle_chain/oracle_chain_report.json
libafl_bfm_fuzz/coverage/verilog_eval_oracle_chain/oracle_chain_report.md
```

唯一失败样例是 `Prob028_m2014_q4a`。该参考 RTL 在 `ena=0` 时不赋值 `q`，会推断
latch：

```systemverilog
always@(*) begin
  if (ena)
    q = d;
end
```

放开 `--allow-latch` 后该样例可以在固定枚举顺序下通过，但生成的是状态轨迹上的
truth table，不是纯粹的输入到输出函数。因此默认拒绝 latch 是正确策略。

按当前可表示集合统计，即排除 latch/时序/大状态空间/`x/z` 后，链路表现为：

| Scope | Passed | Total | Pass Rate |
| --- | ---: | ---: | ---: |
| OracleIR stateless representable set | 54 | 54 | 100% |
| Full VerilogEval dataset | 54 | 156 | 34.6% |

筛出原因主要包括：

| Reason | Count |
| --- | ---: |
| edge-triggered sequential logic | 55 |
| input bits exceed 8 | 39 |
| explicit x/z literal | 7 |

## 结论

当前 ref model 生成、验证、提升和 manifest-style 接入链路是稳定的；在
OracleIR 当前能表达的 stateless 小规模组合逻辑集合上，VerilogEval smoke 测试
没有发现链路错误。

主要限制不在 plugin bundle 或 replay 接入，而在 OracleIR 的表达能力：

- truth-table 方式受输入位宽指数增长限制。
- latch/时序逻辑需要状态建模，不能用当前 `predict(case)` 的 stateless 接口直接表达。
- `x/z` 需要明确的四值逻辑比较策略。

## 后续方向

1. 扩展 OracleIR 的表达式级组合逻辑，减少对全 truth table 的依赖。
2. 在筛选阶段提前识别 latch-like combinational always，避免把 latch 作为 failed
   case 计入可表示集合。
3. 为 sequential ref model 增加显式 stateful oracle 接口，或将其归入 scoreboard DSL。
4. 增加四值逻辑 expected/compare 策略，单独评估含 `x/z` 的 VerilogEval 样例。
