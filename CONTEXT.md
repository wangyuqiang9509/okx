# OKX Quant Bot

一个自用的、在 OKX 现货上交易 BTC、ETH、SOL（均以 USDT 计价）的量化机器人，只做多。有三种 Strategy：Spot Martingale（小资金阶段在跑的激进策略，见 ADR-0005）、Trend Strategy（资金量大后的主策略，每天按趋势和波动率调仓）和 Grid（固定区间网格，保留在代码中）。同一账户同一时间只运行其中一种。

## Language

### 市场与标的

**Instrument（标的）**:
OKX 上一个可交易的现货交易对，以 OKX 的 instId 为唯一标识。每个 Instrument 同一时刻至多一个开着的 Grid；所有 Instrument 共用同一种计价币。
_Avoid_: symbol, pair, ticker, 币种

**Strategy（策略）**:
决定账户里持有多少币的一套确定性规则。本项目有 Grid 和 Trend Strategy 两种。
_Avoid_: bot, robot, algo, 机器人

### 趋势策略

**Trend Strategy（趋势策略）**:
每天用各 Instrument 的日线计算 Weight，并把每个 Sleeve 的持仓调到 Weight 的 Strategy。牛市接近满仓，熊市空仓，趋势不明时部分持仓。
_Avoid_: CTA, 趋势机器人, trend bot

**Book（账簿）**:
Trend Strategy 或 Spot Martingale 名下的 USDT 与币持仓。创建时从 Account Pool 划入，关闭时全部归还给 Account Pool，关闭本身不做任何交易。
_Avoid_: portfolio, account, 账户, 组合

**Sleeve（分仓）**:
Book 权益按 Instrument 等分后的一份。每个 Instrument 只在自己的 Sleeve 内决定持币多少。
_Avoid_: allocation, bucket, 子账户

**Trend Vote（趋势票）**:
对一个 Instrument 的六个二元判断：收盘价是否高于 50、100、200 日均线，是否高于 30、90、180 天前的收盘价。Ensemble 是六票的平均值，取值 0 到 1。
_Avoid_: signal, indicator, 指标

**Target Volatility（目标波动率）**:
风险旋钮。Weight = Ensemble × min(1, Target Volatility ÷ 近 30 日年化波动率)。越高越接近满仓，收益和回撤同比例变大。
_Avoid_: risk level, 风险系数

**Weight（目标权重）**:
一个 Sleeve 中应持有币的价值占比，0 到 1，剩下的是 USDT。
_Avoid_: signal, position size, 仓位比例

**Rebalance（调仓）**:
每天 UTC 日线收盘后，把各 Sleeve 的持仓调向 Weight 的一次动作。偏离不超过 Sleeve 的 5% 或金额低于最小交易额时不交易。日线数据不是最新时推迟，不用旧数据交易。
_Avoid_: sync, 再平衡, 调整

**Decision（决策记录）**:
每次 Rebalance 为每个 Instrument 记下的六票、波动率、Weight、目标价值、当前价值和实际动作，是事后复盘的依据。
_Avoid_: log, signal history, 日志

### 现货倍投

**Spot Martingale（现货倍投）**:
在一个 Instrument 上反复运行 Cycle 的 Strategy：越跌越加倍买入，均价上涨一定比例时全部卖出。只用 Book 里的现金，不加杠杆，不追加资金。
_Avoid_: 马丁, martingale bot, DCA

**Ladder（梯子）**:
一个 Cycle 里按 Level 排开的全部买入：一笔 Opening Buy 加最多 adds 笔 Add，每笔是上一笔的 mult 倍。大小按 Book 现金算好，整架梯子正好花完全部现金。
_Avoid_: grid, 网格, 补仓计划

**Cycle（一轮）**:
从 Opening Buy 到 Take-Profit 全部成交的一次完整过程。结束时的利润留在 Book 里，下一轮按新的现金重新计算 Ladder。
_Avoid_: round, trade, 一单

**Opening Buy（首单）**:
每个 Cycle 开头按市价附近 IOC 买入的第一笔，是 Ladder 的第一个 Level。
_Avoid_: 底仓, Seed Buy

**Add（加仓）**:
价格比上一个 Level 低 step 时成交的买单，常驻在订单簿上，同一时刻至多一张。
_Avoid_: 补仓, DCA order

**Take-Profit（止盈单）**:
卖出全部持币的那张限价单，价格是平均成本 ×（1 + tp）。每次 Add 成交后撤掉并按新的数量和均价重挂。
_Avoid_: TP, 止盈位, 卖单

**Stuck（被套）**:
全部 Add 都已成交、只剩 Take-Profit 在等的状态。此时不再买入，也没有收入，直到价格回到止盈价。
_Avoid_: 爆仓, 满仓, liquidated

### 网格

**Grid（网格）**:
一个 Instrument 上、一个固定的价格区间被切成若干 Level 的整体。区间在启动时确定，运行中不移动；价格离开区间后 Grid 不再产生新的 Order，直到人工重设。
_Avoid_: bot, 机器人

**Level（格）**:
Grid 内的一个价格点。任一时刻每个 Level 上至多一张 Order。成交回报乱序到达时，要挂到仍被占用的 Level 上的对侧 Order 会先暂存，等该 Level 腾出后再挂出。
_Avoid_: line, 网格线, step, tick

**Lower Bound / Upper Bound（下沿 / 上沿）**:
Grid 最低和最高的 Level。跌破下沿时所有买单已成交，持有全部 BTC；涨破上沿时所有卖单已成交，持有全部 USDT。两种情形 Grid 都停在原地。
_Avoid_: 止损位, 止盈位, floor, ceiling

**Spacing（格距）**:
相邻 Level 之间的价差。必须显著大于一次买卖往返的手续费，否则 Grid Profit 为负。
_Avoid_: step, 间隔, gap

**Seed Buy（建仓）**:
Grid 启动时为上方各 Level 的卖单一次性买入所需 BTC 的那笔 Order。它不属于任何 Level，其成本按启动价计入 Ledger，不计入 Grid Profit。
_Avoid_: 初始仓位, 底仓, initial position

**Grid Profit（格利润）**:
一张卖单成交时实现的收益：该卖单与其正下方一格那张买单的价差，扣除双边手续费。每张卖单只对应它下方一格的那张买单，不存在亏本卖出。
_Avoid_: PnL, 收益, 盈利, 套利

### 账户

**Equity（权益）**:
一个 Grid 或 Book 以计价币计算的总价值：它的计价币余额加上所持币按最新价折算的价值。
_Avoid_: balance, NAV, 净值, 总资产

**Position（仓位）**:
某个 Grid 持有的基础币数量，非负。

**Account Pool（账户池）**:
账户里不属于任何开着的 Grid 或 Book 的资金，按币种记账。创建 Grid 或 Book 时从中划出，关闭时剩余资金归还给它。账户实际余额必须等于账户池加上所有开着的 Grid 和 Book 所持有的量，否则就是对账差异。
_Avoid_: idle funds, 闲钱, 余额, offset
_Avoid_: holding, 持仓, balance

### 执行与账本

**Order（订单）**:
平台向 OKX 提交的一次买入或卖出请求，以平台生成的 clOrdId 为唯一标识，生命周期由 OKX 的订单状态驱动。默认形态是 post_only 限价单，常驻订单簿直到成交或被撤销。
_Avoid_: trade, 委托, 挂单

**Fill（成交）**:
一笔 Order 的一次（部分或全部）成交记录，含成交价、成交量、手续费。一个 Order 可对应多个 Fill。
_Avoid_: execution, trade, 成交回报

**Ledger（账本）**:
平台本地记录的全部 Order、Fill 与由此推导的 Position、Equity 和 Grid Profit 历史，是与 OKX 对账的依据。
_Avoid_: database, history, 流水

**Reconciliation（对账）**:
把 Ledger 推导出的各 Grid 状态和 Account Pool 与 OKX 账户实际余额、未成交订单做比对并处理差异的过程。OKX 是事实来源，Ledger 只是历史。计价币对不上时所有 Grid 一起 Halt，某个基础币对不上时只 Halt 对应的 Grid。
_Avoid_: sync, 同步

**Halt（停机）**:
Grid 停止提交新 Order、或 Book 停止 Rebalance 的状态，由对账失败、连接异常或人工命令触发。已挂在 OKX 上的 Order 和已持有的币原样保留，Halt 不等于撤单，更不等于清仓。
_Avoid_: stop, pause, kill, 熔断

### 运行方式

**Replay（回放）**:
把历史 1 分钟 K 线按时间顺序喂给同一份网格引擎、在本地模拟挂单成交的运行方式，用于估算一组参数在过去一段时间的 Grid Profit 和被突破次数。它是引擎的测试夹具，不是独立的回测系统。
_Avoid_: backtest, 回测, simulation, 模拟
