# OKX v5 API 事实调研（2026-09）

来源：https://www.okx.com/docs-v5/en/ 、python-okx、ccxt。标注「未验证」的条目需要在实现前用真实请求确认。

## 端点与模拟盘
- REST 生产：`https://openapi.okx.com`。美/澳（app.okx.com 注册）用 `https://us.okx.com`，欧洲经济区（my.okx.com 注册）用 `https://eea.okx.com`；跨区域 key 不通用（错误码 60032）。
- WS 生产：`wss://ws.okx.com:8443/ws/v5/{public|private|business}`。K 线频道 `candle{bar}` 在 business 端点。
- 模拟盘：同一 REST 域名 + 请求头 `x-simulated-trading: 1`；WS 为 `wss://wspap.okx.com:8443/ws/v5/...`；API key 与实盘分开申请。模拟盘支持现货，官方文档只列出不支持充提等功能。
- `aws.okx.com` 在当前文档已不出现，是否仍可用未验证。

## 鉴权
- 签名：`Base64(HMAC-SHA256(timestamp + method + requestPath + body, secret))`，头 `OK-ACCESS-KEY / OK-ACCESS-SIGN / OK-ACCESS-TIMESTAMP(ISO-8601 ms) / OK-ACCESS-PASSPHRASE`，与服务器时间偏差超过 30 秒即拒绝。
- WS 登录签名串：`timestamp(秒) + "GET" + "/users/self/verify"`。
- 权限：Read / Trade / Withdraw。单 key 最多绑定 20 个 IP；带 trade 权限且未绑 IP 的 key 14 天不用即失效。

## 现货相关模式
- instType `SPOT`，tdMode `cash`，现货市价单须指定 `tgtCcy=base_ccy|quote_ccy`。
- 账户等级 acctLv 1（简单）即可满足现货只做多。

## 订单
- `POST /trade/order`、`batch-orders`（≤20）、`amend-order`、`cancel-order`；可附带 `attachAlgoOrds` 止盈止损。
- ordType：`market, limit, post_only, fok, ioc, optimal_limit_ioc`。
- clOrdId：≤32 位，大小写敏感字母数字。
- 状态：`live, partially_filled, filled, canceled, mmp_canceled`。
- 限速：下单/改单/撤单 60 次/2 秒，按 UID + instId；私有接口按 UID 计，公共接口按 IP 计。子账户总上限 1000 次/2 秒。

## WebSocket
- 公共频道：`tickers, trades, candle{bar}, books, books5, bbo-tbt`。
- 私有频道：`account, positions, balance_and_position, orders, orders-algo`。
- 30 秒无数据服务器断开；客户端发文本 `ping`，期待 `pong`。
- 连接限速：每 IP 每秒 3 次连接请求；每子账户每频道 30 条连接；每连接每小时订阅/退订/登录合计 ≤480。
- 订单簿校验：本地前 25 档 `px:sz` 交替拼接后 CRC32（有符号 32 位）。

## 历史数据
- `GET /market/candles`：只有最近 1440 根，单次最多 300，40 次/2 秒。
- `GET /market/history-candles`：单次最多 100（文档另一处写 300，未验证），20 次/2 秒，可回溯数年。
- 官方批量下载：https://www.okx.com/historical-data ，OHLC 自 2023-07 起，逐笔自 2021-09 起。

## 客户端库
- 无官方 Rust SDK。`rust-okx` 0.6.x 活跃但 pre-1.0；`okx-rs` 2024-05 停更。
- python-okx 0.4.4（2026-09），ccxt 4.5.x。

## 费率
- 普通用户现货约 maker 0.08% / taker 0.10%（官方费率页为前端渲染，未能直接核验），实现时通过 `GET /account/trade-fee` 读取真实费率。
