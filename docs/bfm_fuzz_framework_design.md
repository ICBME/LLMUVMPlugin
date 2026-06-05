# BFM Fuzz Framework Design

本文档已拆分为更清晰的分层文档。请优先阅读：

- [文档总览](README.md)
- [总体架构](architecture/overview.md)
- [Fuzz/Replay 架构](architecture/fuzz_replay.md)
- [Target Manifest 参考](reference/target_manifest.md)
- [插件契约](reference/plugin_contracts.md)
- [Corpus 与 Mutation Directives](reference/corpus_directives.md)
- [接入新 DUT 指南](guides/add_new_dut.md)
- [设计约束](guides/design_constraints.md)

`libafl_bfm_fuzz` 的核心边界保持不变：框架负责 corpus generation、validation、
pyUVM replay、coverage summary 和 generic feedback；DUT 专用协议、reference model、
scoreboard 和 coverage model 必须通过 manifest/plugin 接入。仓库中的 Secworks
target 只作为 smoke/example 插件维护，不改变核心边界。
