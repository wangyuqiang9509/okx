---
status: accepted
---
# 全平台使用 Python 单语言实现

策略为分钟到天级的现货只做多信号策略，对延迟不敏感。曾考虑 Rust 以换取类型安全和未来高频余地，但研究环节（回测分析、画图、调参）在 Rust 生态下代价过高，且 OKX 没有官方 Rust SDK；最终决定数据、回测、实盘、研究全部用 Python 3.12 + asyncio，用 mypy strict 和 pydantic 弥补类型安全。

## Considered Options

- Rust 单语言：否决，研究效率损失过大。
- Python 研究 + Rust 执行：否决，双语言一致性维护成本对个人项目不划算。
