# BFM IR Design

本文档已拆分为更清晰的分层文档。请优先阅读：

- [文档总览](README.md)
- [IR 与运行时架构](architecture/ir_runtime.md)
- [设计约束](guides/design_constraints.md)

`rtlagent_bfm` 的核心边界保持不变：IR 只记录语义名、接口角色、HDL path、
来源信息和可选寄存器布局；协议 timing、reset、handshake、monitor 和
scoreboard 行为属于生成代码或目标插件。
