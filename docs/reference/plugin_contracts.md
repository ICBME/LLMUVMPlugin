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
- 返回 `ReplayResult(actual=..., expected=..., detail=...)`。
- 如果 expected 由 ref model 提供，driver 可以只填 actual。

## Reference Model Plugin

```python
from fuzz_uvm.ref_models import ExpectedResult


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

默认 scoreboard 比较 `record.result.actual` 和 `record.result.expected`。
如果目标处于 smoke replay、只关心无异常执行，应配置自定义 scoreboard。

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
