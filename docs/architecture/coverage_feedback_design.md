# Coverage Feedback Design

本文档描述 coverage feedback 的结构化数据层设计。目标是把工具原始覆盖率
artifact 先归一化为稳定的 coverage export，再派生出可供 heuristic advisor 和
LLM advisor 共同消费的 `rtl_gap`。

当前实现优先覆盖 RTL structural coverage；functional coverage 后续按同一接口扩展。

## 设计目标

- 将 Verilator `.dat` / LCOV `.info` 等工具格式与 advisor 策略解耦。
- 为每个 coverage point 和 RTL gap 提供稳定 `id`，支持跨轮比较和消除已解决 gap。
- 让无 LLM 链路和 LLM 链路消费同一份结构化 `rtl_gap`，避免分叉解析。
- 保留源码上下文和 evidence，既能被规则匹配，也能压缩后放进 LLM prompt。
- 保持旧 coverage summary 字段兼容，避免破坏现有评估脚本。

## 分层数据流

```text
raw coverage artifacts
  .info / .dat / future functional json
        |
        v
coverage_export
  normalized points + totals + stable point ids
        |
        v
rtl_gap_summary
  grouped uncovered RTL gaps + evidence + advisor hints
        |
        +--> heuristic advisor
        |
        +--> LLM prompt/advisor
        |
        v
mutation directives
        |
        v
next corpus generation
```

## 模块职责

`py/fuzz_feedback/coverage_export.py`

- 定义通用 `CoveragePoint` 和 `CoverageExport`。
- 负责 point-level 稳定 `id`、hit/uncovered 判断、按 kind/file/module 汇总。
- 不理解 Verilator、LCOV 或 DUT 语义。
- 后续 functional coverage point 也应复用该数据模型。

`py/fuzz_feedback/rtl_structure_coverage.py`

- 解析 LCOV line coverage 和 Verilator coverage `.dat`。
- 将原始记录归一化为 `CoveragePoint(domain="rtl_structure")`。
- 暴露 `build_rtl_structure_coverage_export()` 作为结构化导出入口。
- 暴露 `build_rtl_structure_coverage()` 作为兼容旧 summary 的入口。

`py/fuzz_feedback/rtl_gap.py`

- 从 uncovered structural `CoveragePoint` 构建 `rtl_gap_summary`。
- 按 `file + line` 聚合同一源码行上的 line/branch/expression/toggle 点。
- 对无 line 的点，按 `file + module + kind + object/name` 聚合。
- 为每个 gap 生成稳定 `id`、源码上下文、evidence 和 advisor hints。
- 负责 gap 排序，不负责生成 mutation directives。

`py/fuzz_feedback/coverage.py`

- 汇总 coverage export、`rtl_gap_summary`、functional coverage 和 stimulus summary。
- 顶层 summary 同时保留 `rtl_structure_coverage` 和 `rtl_gap_summary`，便于 advisor 直接读取。

`py/fuzz_feedback/advisors.py`

- 后续只消费结构化 summary，尤其是 `rtl_gap_summary.top_gaps`。
- 不应解析 `.info` / `.dat`，也不应依赖 Verilator metadata 的细节。

## Coverage Export Schema

`CoverageExport.to_json()` 输出版本化结构：

```json
{
  "schema_version": 1,
  "domain": "rtl_structure",
  "target": "secworks_sha256",
  "sources": {
    "lcov_info": ".../secworks_sha256_coverage.info",
    "verilator_dat": ".../secworks_sha256_coverage.dat"
  },
  "point_count": 100,
  "uncovered_point_count": 12,
  "totals": {
    "total": 100,
    "hit": 88,
    "uncovered": 12,
    "coverage": 0.88
  },
  "by_kind": {},
  "by_file": {},
  "by_module": {},
  "uncovered_points": []
}
```

`CoveragePoint` 的 JSON 形式：

```json
{
  "id": "stable-point-id",
  "domain": "rtl_structure",
  "kind": "branch",
  "file": ".../sha256_core.v",
  "line": 123,
  "count": 0,
  "hit": false,
  "module": "sha256_core",
  "object": "state == CTRL",
  "source": "verilator_dat",
  "name": null,
  "metadata": {}
}
```

字段说明：

- `id`：由 domain/source/kind/file/line/module/object/name 生成的稳定短 hash。
- `domain`：当前为 `rtl_structure`；后续可加入 `uvm_functional`。
- `kind`：结构覆盖类型，例如 `line`、`branch`、`expression`、`toggle`。
- `source`：原始来源，例如 `lcov_info` 或 `verilator_dat`。
- `metadata`：预留给后续工具特有字段，advisor 默认不依赖它。

## RTL Gap Schema

`rtl_gap_summary` 是 advisor 的主要输入：

```json
{
  "schema_version": 1,
  "domain": "rtl_gap",
  "target": "secworks_sha256",
  "total": 8,
  "total_points": 14,
  "priority_order": ["branch", "expression", "line", "fsm", "user", "toggle"],
  "by_kind": {
    "branch": 4,
    "line": 3,
    "toggle": 7
  },
  "by_file": {},
  "by_module": {},
  "top_gaps": []
}
```

单个 gap：

```json
{
  "schema_version": 1,
  "domain": "rtl_gap",
  "id": "stable-gap-id",
  "primary_kind": "branch",
  "file": ".../sha256_core.v",
  "line": 123,
  "module": "sha256_core",
  "code": "if (next) begin",
  "context": [
    {"line": 121, "code": "..."},
    {"line": 122, "code": "..."},
    {"line": 123, "code": "if (next) begin"}
  ],
  "kinds": {
    "branch": 1,
    "line": 1
  },
  "point_count": 2,
  "objects": ["next"],
  "object_count": 1,
  "priority": 100,
  "actionability": "unknown",
  "evidence": {
    "point_ids": ["stable-point-id-a", "stable-point-id-b"],
    "kinds": {"branch": 1, "line": 1},
    "objects": ["next"],
    "sources": ["lcov_info", "verilator_dat"]
  },
  "advisor_hints": [
    {"type": "source_keyword", "value": "next"},
    {"type": "signal_name", "value": "next"}
  ],
  "source": "rtl_structure"
}
```

`primary_kind` 由 gap 内最高优先级 uncovered point 决定。当前优先级为：

```text
branch > expression > line > fsm > user > toggle
```

该顺序刻意降低 toggle 噪声，优先把控制流和表达式 gap 暴露给 advisor。

## Advisor Hints

`advisor_hints` 是无 LLM 链路的规则入口，也是 LLM prompt 的紧凑语义提示。
当前实现从源码行、上下文、module 和 Verilator object 中提取关键词和信号名。

示例 hint：

```json
[
  {"type": "source_keyword", "value": "address"},
  {"type": "source_keyword", "value": "state"},
  {"type": "signal_name", "value": "message_len"}
]
```

无 LLM advisor 可基于 hint 生成保守 directives：

- `len` / `length` / `size` / `pad` / `block`：尝试 hex 长度 bucket。
- `address` / `addr` / `case`：尝试 MMIO 地址或读写组合。
- `mode` / `op` / `key`：尝试 manifest enum/int field cross。
- `reset` / `error` / `default`：降权，或等待 waiver/unreachable 标注。

## Heuristic And LLM Paths

两条链路都应消费同一个 `rtl_gap_summary`：

```text
summary["rtl_gap_summary"]["top_gaps"]
```

无 LLM 链路：

- 使用 `primary_kind`、`advisor_hints`、`module`、`code` 和 manifest schema 做规则匹配。
- 输出 schema-compatible mutation directives。
- 不应读取原始 `.dat` 或 `.info`。

LLM 链路：

- Prompt 中传 top N gaps、manifest schema、stimulus summary 和 heuristic baseline。
- 每个 gap 应包含 `code/context/evidence/advisor_hints`，避免把大量原始 toggle point 塞入 prompt。
- LLM 输出仍必须通过 directive validation 和 corpus validation。

## Backward Compatibility

`build_rtl_structure_coverage()` 继续输出旧字段：

- `totals`
- `by_kind`
- `by_file`
- `by_module`
- `uncovered_points`
- `available_kinds`
- `supported_kinds`

新增字段：

- `coverage_export`
- `rtl_gap_summary`

`coverage.py` 同时将 `rtl_gap_summary` 提升到顶层 summary，方便 advisor 消费：

```json
{
  "rtl_structure_coverage": {
    "coverage_export": {},
    "rtl_gap_summary": {}
  },
  "rtl_gap_summary": {}
}
```

## Functional Coverage Extension

后续 functional coverage 不应直接塞进 `rtl_gap`。建议先导出为同一类
`CoverageExport(domain="uvm_functional")`，例如：

```json
{
  "domain": "uvm_functional",
  "kind": "coverpoint_bin",
  "name": "message_length",
  "object": "56+",
  "count": 0,
  "hit": false
}
```

然后生成独立的 functional gap summary，最后在 advisor 层与 `rtl_gap_summary`
合并推理。例如：

```text
rtl_gap: sha256 next-block branch uncovered
functional_gap: message_length 56+ uncovered
=> generate explicit 56/64-byte message cases
```

这样结构覆盖和功能覆盖保持同一数据模型，但不会混淆 RTL 源码 gap 与目标语义 gap。

## Known Limitations

- 当前 `actionability` 默认为 `unknown`，还没有 waiver/unreachable 机制。
- `advisor_hints` 是轻量关键词抽取，不替代目标专用 coverage advisor plugin。
- `CoverageExport` 默认不导出全量 `points`，避免 summary 过大；调试时可显式请求。
- 结构覆盖仍缺少 per-case attribution，无法判断某个 corpus case 是否贡献了某个新 gap。

## Next Steps

1. 让 heuristic advisor 消费 `rtl_gap_summary.top_gaps`，生成第一版 structural directives。
2. 扩展 Rust generator 支持 variable-length hex directives，例如 `message_lengths`。
3. 增加 waiver/unreachable 标注，避免 defensive/default branch 反复污染反馈。
4. 在 LLM prompt 中加入 compact top gaps，而不是完整 uncovered point 列表。
5. 按同一 `CoverageExport` 模型接入 functional coverage export。
