# DEPLOY.md：在新机器上接手并启动网格机器人

> **给执行本文档的 AI：** 按顺序执行每一步，每步都有「预期结果」，不符合就停下来把输出原样告诉用户，不要自己想办法绕过。
> 唯一需要用户提供的是 `.env` 里的三个值。除此之外所有事都由你完成。
> 真实资金在运行。任何一步不确定时，停下来问用户，而不是猜。

## 背景（执行前读一遍）

- 这是一个在 OKX 现货上同时跑 BTC、ETH、SOL 三个固定区间网格的机器人。词汇见 `CONTEXT.md`，设计决策见 `docs/adr/`。
- 网格的挂单**一直挂在 OKX 上**，机器停了也不会消失，照常成交。
- 本地账本 `data/gridbot.sqlite` 记录网格状态。它**不在 git 里**（含交易数据，刻意不提交）。
- 新机器没有账本，通过 `gridbot recover` 用 API key 从 OKX 订单历史**完整重建**账本：每张挂单、已成交的往返、利润、现金、持币。重建只读不写，不会下单也不会撤单。
- 重建依赖 OKX 最近 7 天的订单历史。**旧机器停机后 7 天内必须完成迁移**，否则 recover 会失败，只能从旧机器拷贝账本文件。

### 当前状态（2026-09-30 写入，迁移完成后此节即过时）

| 网格 | 网格 ID | 资金 | 格距 | 创建时间 UTC |
|---|---|---|---|---|
| BTC-USDT | D8D9828B | 50 USDT | 0.3% | 2026-09-30 07:24 |
| ETH-USDT | 7108B989 | 25 USDT | 0.6% | 2026-09-30 09:46 |
| SOL-USDT | CBE2719D | 25 USDT | 0.6% | 2026-09-30 09:46 |

- 配置文件：`config/validate.toml`（`docker-compose.yml` 默认就用它）。
- 旧机器（Mac）已于 2026-09-30 约 12:00 UTC 停止运行。
- 账户里约 200 USDT 不属于任何网格（账户池）。

## 铁律

1. **同一时间只能有一台机器运行本机器人。** 两台同时跑会各自下单，账本立刻错乱。开始前向用户确认旧机器已停。
2. **绝不打印、回显、提交 `.env` 的内容。** 需要验证时只用 `gridbot doctor`。
3. **不要删除 `data/` 目录**，那是账本。
4. **对账失败导致 Halt 时，不要直接 `resume`。** 把日志里的 `problems` 原样告诉用户。
5. **不要在这个 OKX 账户手动交易 BTC、ETH、SOL，不要充值或提现**，除非用户明确要求，并在之后执行 `gridbot rebaseline`。

## 步骤

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

## 日常操作

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

## 验收与升级

- 三个网格都运行满 24 小时后（2026-10-01 09:46 UTC 之后）执行 `check`。
- 全部 `pass` 时，**先问用户**是否切换到正式配置 `config/multi.toml`（每个币 100 USDT，BTC 格距 1%，ETH、SOL 格距 2%）。
- 用户同意后：

```sh
docker compose down
docker compose run --rm gridbot gridbot -c config/validate.toml cancel-all
sed -i 's#config/validate.toml#config/multi.toml#' docker-compose.yml   # macOS: sed -i ''
docker compose up -d
sleep 20
docker compose logs --tail 60
```

顺序不能换：先停容器再撤单，否则运行中的进程会在撤单过程中继续挂反向单。

**预期：** `cancel-all` 每个币输出 `closed, released ...`。启动后出现三行 `creating:`、三行 `seed attempt 1: filled`，随后 `reconcile (created) ok`。旧网格剩下的 BTC、ETH、SOL 留在账户池里不动，新网格用账户池里的 USDT 建仓。

## 再次迁移到别的机器

1. 在旧机器上：`docker compose down`。
2. 在新机器上从本文档第 0 步开始。7 天内完成即可，无需拷贝任何文件。
