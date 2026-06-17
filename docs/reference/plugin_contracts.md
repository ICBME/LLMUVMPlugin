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

ref model、scoreboard 和 comparator 的公共接口集中在
`fuzz_uvm.contracts`。

```python
from fuzz_uvm.contracts import ComparisonResult, ExpectedResult
```

`ExpectedResult.expected` 可以是字符串、整数、列表、dict 等可比较对象。
`detail` 用于人类可读诊断，`metadata` 用于传递结构化上下文。

`ComparisonResult.passed` 表示当前 record 是否通过。失败时建议填充 `reason`，
这样默认 scoreboard 能在 `check()` 失败时给出稳定错误信息。

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

## Functional Coverage Plugin

```python
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
- `sample_record(record)` 是可选增强入口；如果存在，UVM subscriber 会优先调用它，
  使 coverage model 能看到 `record.result`、`record.error` 和 scoreboard 相关上下文。
- `to_json()` 返回可序列化 summary。

默认 coverage model 统计 schema 字段、可选 manifest coverpoint/cross 和相邻字段
cross。DUT response、协议状态机、错误类型等目标语义覆盖点应放在插件中。

## Constructor 注入

`plugin_loader` 会根据 constructor signature 注入可用参数：

- `config`
- `target`

如果 plugin 接收 `**kwargs`，会得到所有传入参数。建议插件明确声明需要的参数，
避免调试时隐藏拼写错误。
