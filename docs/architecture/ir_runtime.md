# IR and Runtime Architecture

本文档定义 `rtlagent_bfm` 的职责边界。该层只服务于 agent-generated BFM，
不包含具体 DUT 协议。

## 目标

- 记录语义名称、接口角色、真实 HDL path 和来源信息。
- 将 IR binding 解析为 cocotb DUT handle 或 Python test double。
- 为生成的 BFM 提供稳定的语义信号访问 API。

## 模块结构

`rtlagent_bfm.ir`

- `DesignIR`：顶层 IR。
- `InterfaceIR`：接口 role 到 binding name 的映射。
- `SignalBindingIR`：语义名到真实 HDL path 的映射。
- `RegisterIR` / `FieldIR`：通用寄存器布局上下文。

`rtlagent_bfm.loader`

- 从 JSON 或 TOML 加载 IR。
- 不强制引入 YAML 依赖。

`rtlagent_bfm.resolver`

- 根据 `hdl_path` 解析 DUT handle。
- 缺失 required binding 时报告语义名和 HDL path。
- 不进行协议推断、模糊匹配或自动修复。

`rtlagent_bfm.runtime`

- `BfmRuntimeContext`：封装 resolved DUT/IR。
- `GeneratedBfmBase`：生成 BFM 可继承的基础类。
- 提供 `signal()`、`interface_signal()`、`read_value()`、`drive_value()` 等访问方法。

## 核心数据模型

`DesignIR`

- `top`：顶层设计名称。
- `interfaces`：接口集合。
- `bindings`：语义绑定集合。
- `sources`：来源资产。
- `registers`：可选寄存器模型。
- `metadata`：扩展元数据。

`InterfaceIR`

- `name`：接口名。
- `protocol`：协议标签，只作为文本记录。
- `clock` / `reset`：引用 binding name。
- `signals`：接口 role 到 binding name 的映射。
- `metadata`：生成器可用的额外信息。

`SignalBindingIR`

- `name`：稳定语义名。
- `role`：语义角色标签。
- `hdl_path`：真实 DUT 层级路径。
- `width` / `direction` / `active`：可选结构信息。
- `required`：解析失败时是否必须报错。
- `confidence` / `source` / `aliases`：分析追溯信息。

## 运行时契约

生成代码通过语义名访问 DUT：

```python
class GeneratedControlBfm(GeneratedBfmBase):
    async def send_control(self, payload):
        self.ctx.drive_value("req_payload", payload)
        self.ctx.drive_value("req_valid", 1)
```

框架不规定：

- reset sequence。
- 使用哪个 clock edge。
- valid/ready 或其它握手规则。
- polling 和 timeout 策略。
- monitor 和 scoreboard 行为。

这些规则必须由 agent 根据 DUT 文档生成到目标 BFM 或插件中。

## 与 Fuzz/Replay 的关系

典型接入：

1. Agent 生成 IR。
2. Agent 生成 driver plugin。
3. Driver plugin 使用 `BfmRuntimeContext` 或 `GeneratedBfmBase`。
4. Target manifest 的 `driver = "module:Class"` 指向该 plugin。
5. `libafl_bfm_fuzz` 加载 plugin 并回放 JSONL case。
