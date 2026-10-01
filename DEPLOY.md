# DEPLOY.md：在服务器上部署、接手和切换交易机器人

> **给执行本文档的 AI：** 先读「背景」和「铁律」，再按「第一步：判断当前阶段」找到该执行的章节。每步都有「预期结果」，不符合就停下来把输出原样告诉用户，不要自己想办法绕过。
> 唯一需要用户提供的是 `.env` 里的三个值。除此之外所有事都由你完成。
> 真实资金在运行。任何一步不确定时，停下来问用户，而不是猜。

## 背景（执行前读一遍）

- 这是一个在 OKX 现货上交易 BTC、ETH、SOL 的量化机器人，只做多。词汇见 `CONTEXT.md`，设计决策见 `docs/adr/`，研究记录见 `research/`。
- 有两种策略，**同一账户同一时间只运行一种**，程序会强制检查：
  - **网格**（`config/validate.toml`，命令 `start`）：目前在跑的三个小额验证网格，用来验证下单和对账在实盘上可靠。
  - **趋势策略**（`config/trend.toml`，命令 `trend-run`）：主策略，每天 UTC 00:05 按趋势和波动率调仓，见 `docs/adr/0004-trend-strategy-replaces-grid.md`。
- 用户已经决定的路线：网格跑满 24 小时并通过 `check` 验收后停掉，全部资金交给趋势策略，目标波动率 40%。**切换前必须让用户看过 `trend-plan` 的输出并确认。**
- 网格的挂单**一直挂在 OKX 上**，机器停了也不会消失，照常成交。
- 本地账本 `data/gridbot.sqlite` 记录所有状态。它**不在 git 里**（含交易数据，刻意不提交）。
- 没有账本的新机器，用 `gridbot recover` 从 OKX 订单历史**完整重建**网格账本。重建只读不写，依赖 OKX 最近 7 天的订单历史，所以**旧机器停机后 7 天内必须完成迁移**（旧机器 Mac 于 2026-09-30 约 12:00 UTC 停机）。

### 时间线

| 时间（UTC） | 事件 |
|---|---|
| 2026-09-30 07:24 | BTC 验证网格创建（D8D9828B，50 USDT，格距 0.3%） |
| 2026-09-30 09:46 | ETH、SOL 验证网格创建（7108B989、CBE2719D，各 25 USDT，格距 0.6%） |
| 2026-09-30 约 12:00 | 旧机器 Mac 停机，网格挂单留在 OKX 上 |
| 2026-10-01 09:46 之后 | 可以运行 `check` 验收三个网格 |
| 验收通过 + 用户确认后 | 切换到趋势策略 |
| 2026-10-07 约 12:00 | `recover` 的最后期限 |

账户里另有约 200 USDT 不属于任何网格（账户池），切换时会连同网格剩余的资金一起交给趋势策略。

## 铁律

1. **同一时间只能有一台机器运行本机器人。** 两台同时跑会各自下单，账本立刻错乱。开始前向用户确认旧机器已停。
2. **绝不打印、回显、提交 `.env` 的内容。** 需要验证时只用 `gridbot doctor`。
3. **不要删除 `data/` 目录**，那是账本。
4. **对账失败导致 Halt 时，不要直接 `resume`。** 把日志里的 `problems` 原样告诉用户。
5. **不要在这个 OKX 账户手动交易 BTC、ETH、SOL，不要充值或提现**，除非用户明确要求，并在之后执行 `gridbot rebaseline`。

## 第一步：判断当前阶段

在仓库目录里运行（仓库还不存在就直接是阶段 A）：

```sh
docker ps --filter name=gridbot --format '{{.Names}} {{.Status}}'
ls data/gridbot.sqlite 2>/dev/null
grep command docker-compose.yml
```

| 看到的情况 | 阶段 | 执行 |
|---|---|---|
| 这台机器上还没有仓库，或没有 `data/gridbot.sqlite` | A：首次部署 | 下面「步骤」0 到 7，完成后网格在这台机器上运行 |
| 容器在运行，`command` 里是 `validate.toml` 和 `start` | B：网格在运行 | 先做「更新代码」，再看是否已到验收时间 |
| 容器在运行，`command` 里是 `trend.toml` 和 `trend-run` | C：趋势策略在运行 | 只需要「更新代码」和「日常操作」 |

### 更新代码（阶段 B、C）

```sh
git pull
docker compose up -d --build
sleep 30
docker compose logs --tail 40
```

重建镜像会重启容器，这是安全的：挂单和持仓都在 OKX 上，账本在 `data/` 里，进程启动时会先对账并补上停机期间的成交。

**预期：** 阶段 B 出现三行 `resuming (active)` 和 `reconcile (startup) ok`；阶段 C 出现 `[trend ...] resuming` 和 `reconcile (startup) ok`。

阶段 B 更新完后：如果用户要求立即切换，直接按「从网格切换到趋势策略」操作；否则如果当前时间已过 2026-10-01 09:46 UTC，执行「验收」，通过后再切换；还没到就告诉用户验收时间，然后结束。

## 步骤（阶段 A：首次部署）

### 0. 确认前提

向用户确认两件事：
- 旧机器上的机器人已经停止。
- 这台机器能访问 `www.okx.com`。

```sh
curl -s -o /dev/null -w "%{http_code}\n" https://www.okx.com/api/v5/public/time
```

**预期：** 输出 `200`。

### 1. 获取代码

```sh
git clone https://github.com/wangyuqiang9509/okx.git
cd okx
```

### 2. 准备 `.env`（唯一需要用户操作的步骤）

```sh
cp .env.example .env
chmod 600 .env
```

然后请用户自己打开 `.env` 填写三个值，**不要让用户把值发给你**：

| 变量 | 含义 |
|---|---|
| `OKX_API_KEY` | OKX「API 管理」页面上显示的 API Key |
| `OKX_SECRET_KEY` | 创建 key 时只显示一次的 Secret Key |
| `OKX_PASSPHRASE` | 创建 key 时用户自己设的 Passphrase（不是登录密码） |

key 的权限必须是「读取 + 交易」，不要提现。如果 key 绑定了 IP，需要把这台机器的公网 IP 加进去：

```sh
curl -s https://ifconfig.me; echo
```

用户确认填好后再继续。

### 3. 选择运行方式并构建

优先用 Docker：

```sh
docker compose version && docker compose build
```

**预期：** 构建成功。以下命令都以 Docker 形式给出。

没有 Docker 时改用 Python 3.12 + uv，之后所有 `docker compose run --rm gridbot gridbot ...` 替换为 `.venv/bin/gridbot ...`：

```sh
uv venv --python 3.12 .venv && uv pip install -p .venv/bin/python -e ".[dev]"
.venv/bin/pytest -q
```

### 4. 体检

```sh
docker compose run --rm gridbot gridbot -c config/validate.toml doctor
```

**预期：**

```
credentials OK (read + trade)
balances: ...
BTC-USDT: 20 live grid orders on OKX, not in ledger
ETH-USDT: 16 live grid orders on OKX, not in ledger
SOL-USDT: 16 live grid orders on OKX, not in ledger
NEXT: run `gridbot recover`, then start
```

挂单数量会因成交而变化，不必完全等于 20/16/16。

**如果失败：** 输出会给出 `CREDENTIALS FAILED` 和可能原因，例如 IP 不在白名单、Passphrase 错误。告诉用户具体原因，请他修改 `.env` 或 OKX 上的 key 设置后，重新执行本步。

**如果显示 `NEXT: ledger is partial`：** 停下，告诉用户，不要继续。

**如果显示 `ledger matches`：** 说明 `data/` 里已经有账本（比如之前恢复过），跳到第 6 步。

### 5. 从 OKX 重建账本

```sh
docker compose run --rm gridbot gridbot -c config/validate.toml recover
```

**预期：** 每个币一行 `recovered, N resting orders matched`，最后一行 `RECOVER OK. Next: start the runner.`

**如果输出 `RECOVER FAILED`：** 停下，把整段输出告诉用户。不要执行 `start`：在空账本上 `start` 会被程序拒绝，因为它检测到 OKX 上已有网格挂单。

### 6. 启动

```sh
docker compose up -d
sleep 20
docker compose logs --tail 40
```

**预期日志里依次出现：**
- 三行 `resuming (active) with N resting orders`
- `reconcile (startup) ok:`，其中 `balance_diff` 的四个值都接近 0（例如 `0E-9`、`-1E-14`）
- `ws connected and subscribed to orders BTC-USDT,ETH-USDT,SOL-USDT`
- `reconcile (ws_connect) ok:`

**如果出现 `HALT` 或 `problems`：** 按铁律 4 处理，不要 resume。

停机期间 OKX 上发生的成交，会在这一步自动补上，并挂出对应的反向单，日志里会有 `catch-up fill` 或 `placed` 行。这是正常的。

### 7. 确认运行状态

```sh
docker exec gridbot gridbot -c config/validate.toml status
```

**预期：** 三个网格都是 `active`，每个网格有 `reconcile ... ok=True`，最后一行是账户池余额。

把这段输出摘要告诉用户：三个网格的状态、已完成往返次数、已实现利润。

阶段 A 到此完成。接下来和阶段 B 一样：如果当前时间已过 2026-10-01 09:46 UTC，执行「验收」；还没到就告诉用户验收时间，然后结束。

## 日常操作（网格阶段）

| 目的 | 命令 |
|---|---|
| 看状态 | `docker exec gridbot gridbot -c config/validate.toml status` |
| 看某个币每一格 | `docker exec gridbot gridbot -c config/validate.toml status --inst ETH-USDT` |
| 实时日志 | `docker compose logs -f` |
| 暂停下新单（挂单保留） | `docker exec gridbot gridbot -c config/validate.toml halt` |
| 恢复 | `docker exec gridbot gridbot -c config/validate.toml resume` |
| 撤销某币全部网格单并关闭该网格 | `docker exec gridbot gridbot -c config/validate.toml cancel-all --inst SOL-USDT`，之后该币不再交易，要重建需重启容器 |
| 充值或提现之后 | `docker exec gridbot gridbot -c config/validate.toml rebaseline`，然后 `resume` |
| 升级验收（见下） | `docker exec gridbot gridbot -c config/validate.toml check` |

## 验收

- 三个网格都运行满 24 小时后（2026-10-01 09:46 UTC 之后）执行：

```sh
docker exec gridbot gridbot -c config/validate.toml check
```

- 全部 `pass` 说明执行层（下单、成交回报、补单、对账）在实盘上可靠。下一步不是扩大网格，而是按下一节切换到趋势策略。
- 有 `FAIL` 时把输出原样告诉用户，不要切换。

## 从网格切换到趋势策略

用户已决定（见 `docs/adr/0004-trend-strategy-replaces-grid.md`）：网格验收通过后停掉，全部资金交给趋势策略，目标波动率 40%。

**前提（满足其一）：**
- `check` 已全部 `pass`，并且**向用户确认过**现在切换；或者
- 用户明确要求不等验收直接切换（2026-10-01 用户已提出这一要求，理由是执行层已有足够实盘证据：73 笔成交，`recover` 重建的挂单与交易所完全一致）。这时先运行 `status`，确认三个网格都是 `active`、最近一次对账 `ok=True`，再继续。有 `HALT` 或对账失败就停下来告诉用户。

```sh
docker exec gridbot gridbot -c config/validate.toml status
```

```sh
# 1. 停掉网格进程，撤掉全部网格单并关闭网格（持有的币留在账户池里，不卖）
docker compose down
docker compose run --rm gridbot gridbot -c config/validate.toml cancel-all

# 2. 预览今天的趋势信号和将要下的单（只读，不下单）
docker compose run --rm gridbot gridbot -c config/trend.toml trend-plan
```

**预期：** 每个币一行六票、波动率和 Weight；下面是 `plan for hypothetical book from the account pool`，列出每个币的目标和买卖动作。把这段输出给用户看，**用户确认后**再继续。

```sh
# 3. 把容器的命令换成趋势策略并启动
sed -i 's#"--config", "config/[a-z]*.toml", "start"#"--config", "config/trend.toml", "trend-run"#' docker-compose.yml   # macOS: sed -i ''
grep command docker-compose.yml
docker compose up -d
sleep 30
docker compose logs --tail 40
```

**预期：** `grep` 显示 `config/trend.toml` 和 `trend-run`。日志里依次出现：`created with ... USDT and ...`、`reconcile (startup) ok`、每个币一行 `weight ... target ... -> buy/sell ...`、每笔成交一行、`reconcile (rebalance) ok`。

之后每天 UTC 00:05 自动调仓一次。日常命令：

| 目的 | 命令 |
|---|---|
| 看状态和最近的决策 | `docker exec gridbot gridbot -c config/trend.toml trend-status` |
| 看今天的信号（只读） | `docker exec gridbot gridbot -c config/trend.toml trend-plan` |
| 暂停调仓（持仓不动） | `docker exec gridbot gridbot -c config/trend.toml trend-halt` |
| 恢复 | `docker exec gridbot gridbot -c config/trend.toml trend-resume` |
| 充值或提现之后 | 先 `trend-halt`，用 `gridbot -c config/validate.toml rebaseline` 同步账户池，再 `trend-resume` |

对账失败会自动暂停调仓，处理方式同铁律 4。

## 再次迁移到别的机器

**同一时间只能有一台机器运行。** 无论哪种策略，都是先停旧机器，再在新机器上启动。

### 迁移网格（还没切换到趋势策略时）

1. 在旧机器上：`docker compose down`。
2. 在新机器上按「步骤（阶段 A）」从第 0 步开始。7 天内完成即可，无需拷贝任何文件。

### 迁移趋势策略（已切换之后）

趋势策略的全部状态就是账户里的 USDT 和币，新机器上不需要重建。空账本启动时，它会把账户里的全部 USDT、BTC、ETH、SOL 接收为自己的持仓，然后照常每天调仓。

1. 在旧机器上：`docker compose down`。
2. 在新机器上按阶段 A 的第 0 到 3 步准备代码、`.env` 并构建镜像。
3. 把 `docker-compose.yml` 的命令改成趋势策略，预览，然后启动：

```sh
sed -i 's#"--config", "config/[a-z]*.toml", "start"#"--config", "config/trend.toml", "trend-run"#' docker-compose.yml   # macOS: sed -i ''
docker compose run --rm gridbot gridbot -c config/trend.toml trend-plan
docker compose up -d
sleep 30
docker compose logs --tail 40
```

**预期：** `trend-plan` 显示 `hypothetical book` 的现金和持仓与 OKX 账户一致；启动后出现 `[trend ...] created with ...` 和 `reconcile (startup) ok`。

代价是旧机器上的决策记录和盈亏基准不会带过来，新账本从迁移这一刻重新计算盈亏。

**不要在迁移后的新机器上运行 `recover` 或 `start`**，那是网格用的。
