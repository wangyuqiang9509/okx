---
status: superseded by ADR-0004
---
# 项目范围收窄为 BTC-USDT 现货固定区间网格机器人

原设想是一个多策略信号平台：Strategy 输出目标权重 Signal，Portfolio 按 Allocation 合并后 Rebalance。2026-09-30 决定放弃该愿景，项目就是一个固定区间网格机器人。原因：网格由价格档位和常驻挂单定义、必须知道自己有多少资金，无法表达为 0-1 的目标权重；而平台尚无一行代码，为它改契约或并列一个第二上下文都是在为不存在的东西付设计费。Strategy、Signal、Allocation、Portfolio、Rebalance、Risk Limit、Universe、Bar、Timeframe 已从词汇表删除。

## Considered Options

- 网格作为独立限界上下文与平台并列：否决，平台本身不存在，并列没有意义。
- 改写 Strategy/Signal 契约让策略可以直接产生 Order：否决，同上。

## Consequences

- ADR-0001 的 Python 选择继续有效：研究效率的理由不再重要，但 python-okx SDK 与 asyncio 仍是最省事的路径。
- 如果将来要加信号策略，需要重新设计，而不是在网格代码上生长。
