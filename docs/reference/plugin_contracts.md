# Plugin Contracts

目标专用逻辑通过 Python plugin 接入。Plugin spec 格式统一为：

```text
module:Object
```

例如：

```toml
driver = "my_project.my_driver:MyDriver"
```

## Driver Plugin

driver 是唯一直接驱动 DUT 协议的组件。

```python
from fuzz_bfm.bfm_base import ReplayResult


class MyDriver:
    def __init__(self, config=None):
        self.config = config

    async def reset(self) -> None:
        ...

    async def execute(self, case) -> ReplayResult:
        ...
```

要求：

- `reset()` 在 replay 开始前调用一次。
- `execute(case)` 将一个 `FuzzCase` 转换为 DUT transaction。
- 返回 `ReplayResult(actual=..., expected=..., detail=..., metadata=...)`。
- 如果 expected 由 ref model 提供，driver 可以只填 actual。

## 通用结果类型

ref model、comparator、scoreboard 和 coverage 的公共接口集中在
`fuzz_uvm.contracts`。

```python
from fuzz_uvm.contracts import ComparisonResult, ExpectedResult
```

`ExpectedResult.expected` 可以是字符串、整数、列表、dict 等可比较对象。
`detail` 用于人类可读诊断，`metadata` 用于传递结构化上下文。

`ComparisonResult.passed` 表示当前 record 是否通过。失败时建议填充 `reason`，
这样默认 scoreboard 能在 `check()` 失败时给出稳定错误信息。

`metadata`、`summary()` 和 `to_json()` 必须能被 `json.dumps()` 序列化。

## LLM 生成物硬约束

LLM 生成物应是纯 Python plugin，不应生成 UVM component。所有生成类都使用统一
constructor：

```python
def __init__(self, target=None, config=None):
    self.target = target
    self.config = config
```

禁止事项：

- 不继承 `uvm_component`，不直接 import/use `cocotb` 或 DUT handle。
- 不依赖仿真时间，不调用 `Timer`、`RisingEdge`、`time.sleep` 等等待逻辑。
- 不访问网络、进程或文件系统。
- 不在 module import 阶段执行业务逻辑。
- 不返回不可 JSON 序列化的 `metadata`、`summary()` 或 `to_json()`。

推荐生成顺序：

1. 优先生成 `ReferenceModelPlugin`。
2. 如默认 `actual == expected` 不足，生成 `ComparatorPlugin`。
3. 只有乱序、多 transaction 状态、多通道匹配等情况才生成完整 `ScoreboardPlugin`。
4. 覆盖率需求独立生成 `FunctionalCoveragePlugin`。

## 生成物接入拓扑

LLM/codegen 不直接改写 replay runtime。生成物先进入 pipeline 的显式 Connector 链路：

```text
generation_context -> generated_artifact_bundle
generated_artifact_bundle -> plugin_contract_validator
plugin_contract_validator -> plugin_registry
plugin_registry -> target_manifest_overlay
target_manifest_overlay -> target_manifest
target_manifest -> ref_model / comparator / scoreboard / functional_coverage
comparator -> scoreboard
```

初版 Python 接口位于 `fuzz_pipeline.generated_plugins`：

- `GeneratedPluginBundle`：LLM/codegen 输出的目标名和 plugin spec 集合。
- `GeneratedPluginValidationReport`：契约校验结果。
- `GeneratedPluginRegistry`：验证通过后允许进入 manifest overlay 的 plugin refs。
- `TargetManifestOverlay`：最终写入 `ref_model`、`comparator`、`scoreboard`、
  `coverage_model` 等 manifest 字段的更新。

推荐的 bundle JSON 形态：

```json
{
  "target": "my_dut",
  "plugins": {
    "ref_model": "generated.my_dut_ref_model:MyRefModel",
    "comparator": "generated.my_dut_comparator:MyComparator",
    "scoreboard": "fuzz_uvm.scoreboards:ResultScoreboard",
    "coverage_model": "generated.my_dut_coverage:MyCoverageModel"
  },
  "metadata": {
    "generator": "llm"
  }
}
```

`plugins` 只允许使用这些 role：`ref_model`、`comparator`、`scoreboard`、
`coverage_model`。每个值必须是 `module:Object`。`validate_generated_plugin_bundle()`
默认会 import 并实例化 plugin，然后调用对应契约 validator；如只做静态检查，可传入
`import_plugins=False`。

生成物链路推荐持久化以下 JSON artifact，均可由 `fuzz_pipeline.generated_plugins` 读取：

- `generated_artifact_bundle.json`
- `plugin_validation_report.json`
- `plugin_registry.json`
- `target_manifest_overlay.json`

`ReplayStageAdapter` 默认会先通过 `manifest_to_comparator` 构建 comparator，再通过
`comparator_to_scoreboard` 将其注入 scoreboard 构建；没有配置自定义 comparator 时，该
步骤仍会产生可观测事件，但 scoreboard 会回退到默认 equality comparator。

## Reference Model Plugin

```python
from fuzz_uvm.contracts import ExpectedResult


class MyRefModel:
    def __init__(self, target=None, config=None):
        ...

    def predict(self, case) -> ExpectedResult:
        ...
```

要求：

- 不驱动 DUT。
- 不依赖仿真时间。
- 只根据 case 计算 expected。
- `predict()` 推荐返回 `ExpectedResult`。框架也会归一化带 `expected` 字段的
  mapping 或具有 `expected` 属性的对象。

## Scoreboard Plugin

```python
class MyScoreboard:
    def __init__(self, target=None, config=None):
        ...

    def write(self, record) -> None:
        ...

    def check(self) -> None:
        ...

    def summary(self) -> dict:
        ...
```

默认 scoreboard 使用 `DefaultComparator` 比较 `record.result.actual` 和
`record.result.expected`。如果 ref model 返回了 `detail` 或 `metadata`，运行时会将
它们放入 `record.result.metadata["ref_model"]`。

如果目标处于 smoke replay、只关心无异常执行，应配置自定义 scoreboard。

## Comparator Plugin

简单目标通常不需要自定义 scoreboard，只需要替换比较规则。Comparator 是一个轻量
接口：

```python
from fuzz_uvm.contracts import ComparisonResult


class MyComparator:
    def compare(self, actual, expected, record) -> ComparisonResult:
        if normalize(actual) == normalize(expected):
            return ComparisonResult(passed=True)
        return ComparisonResult(
            passed=False,
            reason=f"actual={actual!r} expected={expected!r}",
        )
```

`ResultScoreboard(target, comparator=MyComparator())` 会使用该 comparator。完整自定义
scoreboard 仍然适合乱序响应、跨 transaction 状态检查、多通道协议等场景。

Manifest 中可通过 `comparator` 字段接入：

```toml
scoreboard = "fuzz_uvm.scoreboards:ResultScoreboard"
comparator = "generated.my_dut_comparator:MyComparator"
```

## Functional Coverage Plugin

```python
from fuzz_uvm.contracts import FunctionalCoveragePlugin


class MyCoverageModel:
    def __init__(self, target=None, config=None):
        self.target = target

    def sample(self, case) -> None:
        ...

    def sample_record(self, record) -> None:
        ...

    def to_json(self) -> dict:
        ...
```

要求：

- `sample(case)` 是兼容入口，只依赖 stimulus `FuzzCase`。
- `sample_record(record)` 是 replay 入口；如果不需要 result/error 上下文，可以在其中
  简单转调 `sample(record.case)`。
- `to_json()` 返回可序列化 summary。

默认 coverage model 统计 schema 字段、可选 manifest coverpoint/cross 和相邻字段
cross。DUT response、协议状态机、错误类型等目标语义覆盖点应放在插件中。

## Constructor 注入

`plugin_loader` 会根据 constructor signature 注入可用参数：

- `config`
- `target`

如果 plugin 接收 `**kwargs`，会得到所有传入参数。建议插件明确声明需要的参数，
避免调试时隐藏拼写错误。
