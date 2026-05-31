# Corpus and Mutation Directives

本文档定义 JSONL corpus 和 generic mutation directives 的稳定格式。

## JSONL Corpus

每一行是一条独立 JSON object：

```json
{"target": "my_dut", "op": "write", "addr": 16, "payload": "00112233", "origin": "libafl_seed"}
```

约束：

- `target` 必须等于 manifest `name`。
- 每个 `[[field]]` 必须存在并满足 field schema。
- `hex` 字段使用无前缀十六进制字符串。
- `origin` 可选，用于覆盖和调试统计。

## FuzzCase

Python replay 中 corpus line 会被包装成：

```python
FuzzCase(target="my_dut", data={...}, line_no=1)
```

目标 driver、ref model、scoreboard 和 coverage plugin 都以该对象为主要输入。

## Mutation Directives

generic directive 示例：

```json
{
  "directives": [
    {
      "target": "my_dut",
      "name": "cover_write_edges",
      "reason": "Exercise sparse write cases",
      "op_values": ["write"],
      "addr_values": [0, 4095],
      "payload_patterns": ["zero", "ff", "increment"]
    }
  ]
}
```

约定：

- `<field>_values`：为任意字段指定候选值。
- `<hex_field>_patterns`：为 hex 字段指定 byte pattern。
- `cases`：直接给出显式完整或部分 case。
- 不认识的 directive key 会被忽略。
- 无法按 schema 解析的值会被忽略。

## 显式 Case

```json
{
  "directives": [
    {
      "target": "my_dut",
      "name": "directed_write",
      "cases": [
        {"op": "write", "addr": 0, "payload": "00000000"}
      ]
    }
  ]
}
```

显式 case 可以只提供部分字段。缺失字段由 generator 使用 schema default 补齐。

## Hex Patterns

通用 pattern：

- `zero`
- `ff`
- `increment`
- `decrement`
- `alternating`
- `walking_one`

pattern 生成的 byte length 来自 field 的 `hex_len` 或 `hex_len_by`。
