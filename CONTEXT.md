# OKX Grid Bot

一个自用的、在 OKX 现货上同时运行多个固定区间网格的交易机器人（目前 BTC、ETH、SOL 各一个，均以 USDT 计价）：把一段价格区间切成若干格，每格常驻一张限价单，低买高卖赚取震荡差价。价格离开区间后不追、不止损，最差情形是满仓持有该币。

## Language

### 市场与标的

**Instrument（标的）**:
OKX 上一个可交易的现货交易对，以 OKX 的 instId 为唯一标识。每个 Instrument 同一时刻至多一个开着的 Grid；所有 Instrument 共用同一种计价币。
_Avoid_: symbol, pair, ticker, 币种

### 网格

**Grid（网格）**:
一个 Instrument 上、一个固定的价格区间被切成若干 Level 的整体。区间在启动时确定，运行中不移动；价格离开区间后 Grid 不再产生新的 Order，直到人工重设。
_Avoid_: strategy, 策略, bot, 机器人

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
一个 Grid 以计价币计算的总价值：它的计价币余额加上 Position 按最新价折算的价值。
_Avoid_: balance, NAV, 净值, 总资产

**Position（仓位）**:
某个 Grid 持有的基础币数量，非负。

**Account Pool（账户池）**:
账户里不属于任何开着的 Grid 的资金，按币种记账。创建 Grid 时从中划出本金，关闭 Grid 时剩余资金归还给它。账户实际余额必须等于账户池加上所有开着的 Grid 所持有的量，否则就是对账差异。
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
平台停止提交任何新 Order 的状态，由对账失败、连接异常或人工命令触发。已挂在 OKX 上的 Order 原样保留，Halt 不等于撤单，更不等于清仓。
_Avoid_: stop, pause, kill, 熔断

### 运行方式

**Replay（回放）**:
把历史 1 分钟 K 线按时间顺序喂给同一份网格引擎、在本地模拟挂单成交的运行方式，用于估算一组参数在过去一段时间的 Grid Profit 和被突破次数。它是引擎的测试夹具，不是独立的回测系统。
_Avoid_: backtest, 回测, simulation, 模拟
