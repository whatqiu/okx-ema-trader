# OKX EMA Trader

EMA20/EMA60 + ADX 的趋势跟随策略，跑在 OKX 永续合约上。

**下单只走 OKX Demo 模拟盘**：`credentials.py` 拒绝非 demo profile，`Broker` 强制 sandbox
并校验 `x-simulated-trading` 头，因此不存在"误连实盘"的路径。回测与实盘调用同一份
`strategy.classify()`，不会漂移。

只读公共行情（回测、盯信号、查价格）不需要 API Key；只有 executor 和控制台的交易功能需要
demo 凭证。

## 运行

```bat
cd /d F:\Claude\okx-ema-trader
.venv\Scripts\activate
set PYTHONPATH=src
python -m okx_ema_trader.main
```

依赖见 `requirements.txt`（画图用的 `matplotlib` / `mplfinance` 是可选依赖，见文末）。

> 根目录那几个 `.bat` 一键启动器已清理，直接用命令即可，等价关系：
> - 盯信号：`python -m okx_ema_trader.monitor --symbol AAVE-USDT-SWAP`
> - 回测：`python -m okx_ema_trader.backtest --days 90`
> - 自动交易 demo 盘：`python -m okx_ema_trader.executor`（先加 `--dry-run`，只观察不下单）

## 数据链路

1. 启动时先通过 REST `GET /api/v5/market/candles` 拉 200 根历史 K 线预热，指标立即可用，无需等十几个小时。
2. 再连 WebSocket `wss://ws.okx.com:8443/ws/v5/business` 订阅 `candle5m` / `candle15m`。
   > K 线频道在 **business** 端点，public 端点会返回 `60018 channel doesn't exist`。
3. 每次重连都会重新预热一次，补齐断线期间收掉的 K 线。

## 当前信号逻辑

**15m 提供方向环境（状态），5m 提供入场时机（事件）。** 这是最容易搞错的一点：
15m 看的是"现在处在什么状态"，5m 看的是"刚刚有没有发生交叉"。

四个条件必须同时成立：

| # | 周期 | 条件 | 不成立时的原因码 |
|---|---|---|---|
| 1 | 15m | ADX **严格大于** `adx_min`（默认 20，不是 25） | `adx_too_low` |
| 2 | 15m | EMA20 与 EMA60 分出方向（相等则无环境） | `no_trend_env` |
| 3 | 15m | `abs(close - EMA20) / EMA20 <= 0.02`（偏离度） | `deviation_too_large` |
| 4 | 5m | **刚刚发生**金叉 / 死叉 | `no_cross_event` |

第 4 条是事件不是状态。判定方式是拿上一根和这一根比：

```python
# 多头：上一根 fast <= slow，这一根 fast > slow  → 金叉那一瞬间
prev_fast <= prev_slow and fast > slow
```

也就是说，5m 图上 EMA20 一直在 EMA60 上方，是**没有**信号的 —— 只有它从下方穿上去的那
一根才触发。一直持有多头状态不会每根 K 线重复发信号。

偏离度过滤只作用于 15m、只在**新开仓**时生效，不影响已经持有的仓位（否则价格走远一点
就会被误当成出场信号）。

参数都在 `config/config.yaml`：`adx_min: 20`、`deviation_max: 0.02`、
`ema_fast: 20`、`ema_slow: 60`、`adx_period: 14`。判定逻辑只有一份，在
`strategy.py` 的 `classify()`；回测和实盘调用的是同一个函数（测试里用对象身份断言钉住）。

## 第二阶段之前：先测有没有 edge（`backtest.py`）

接任何下单逻辑之前，必须先回答一个问题：**这套 EMA 交叉到底能不能赚回手续费和滑点。**

```bat
cd /d F:\Claude\okx-ema-trader
.venv\Scripts\activate
set PYTHONPATH=src
python -m okx_ema_trader.backtest --days 90
```

可选参数：`--fee-bps`（单侧手续费，默认 5.0）、`--slippage-bps`（默认 3.0）、`--days`、`--symbol`、
`--show-trades`（明细列出多少笔，默认 20，0 = 全部）。
把两个 cost 都设成 0 可以看"毛利"，用来判断它是"被成本吃掉了"还是"根本没 alpha"。

**成交模型：信号在 K 线 N 收盘时产生，在 K 线 N+1 的开盘价成交。**
K 线 N 的收盘价不作为成交价使用——那是你看到信号之后才知道的价格，不可能成交在它上面。
如果 K 线 N+1 不存在（信号出现在最后一根），这笔信号直接丢弃、不记账（输出里的 `unfilled signals`）。

明细表把"决策时间"和"成交时间"分开列：
`entry_signal_time` / `entry_execution_time` / `entry_price`、
`exit_signal_time` / `exit_execution_time` / `exit_price` / `exit_reason`。
止损成交两者相同（止损在被触及的那根 K 线上判定），信号/强平则相差一根。

输出里最重要的一行是 **`BREAKEVEN cost`**：
超过这个 bps 策略就归零。如果它低于 5，说明这策略连交易所手续费都覆盖不了。

**回撤有两个数字，不要混用：**

- `max drawdown (1x)` —— 名义本金口径。把策略当成"1 单位资金做 1 单位仓位"，
  描述的是**信号本身的波动**。
- `max drawdown`（账户口径）—— 按 `config.yaml` 里的 `equity_pct × leverage` 换算到账户，
  超过 100% 会封顶，并单独给出 `account wiped out` 和 `ruin leverage`。
  `ruin leverage` = 满仓时超过多少倍杠杆这轮就会归零。

这两个数字差很多。`equity` 序列记录的是**累计盈亏除以名义本金**，起点是 0，
它的峰值是"赚了多少"，不是"本金有多少"。拿盈亏波动除以盈亏峰值会算出荒谬的数字
（曾经输出过 284%），所以换算时必须显式带上账户敞口。

只读公共行情，不需要 API Key。`--proxy` 默认取 Windows 系统代理（Internet 选项里那个）；
传 `--proxy ""` 走直连。

## Web 控制台（`console_server.py` + `web/index.html`）

本地浏览器操作入口，四个功能区：**查询 / 交易 / 回测 / 设置**。纯标准库实现（`http.server`），
不新增依赖；只监听 `127.0.0.1`，数据不出本机。

```bat
start-console.bat
```
或
```bat
set PYTHONPATH=src
python -m okx_ema_trader.console_server --port 8787 --open
```

- **查询**：最新价 / 24h 涨跌（红涨绿跌）、当前信号与四道闸门（ADX、偏离度、15m 方向、5m 交叉）、
  5m/15m 指标明细、Demo 持仓与浮盈。只用已收盘 K 线，和实盘判断完全一致。
- **交易**：做多 / 做空 / 平仓，可设名义金额、杠杆、止损%。默认勾选「空跑」。
  **只允许 Demo 模拟盘**：`credentials` 拒绝非 demo profile，`Broker` 强制 sandbox 并校验
  `x-simulated-trading` 头；开仓把止损写进同一个请求，止损没挂上会自动平掉。
- **回测**：选币种 / 天数 / 手续费 / 滑点，输出汇总和成交明细
  （`entry_signal_time` / `entry_execution_time` / `entry_price` / `exit_signal_time` /
  `exit_execution_time` / `exit_price` / `exit_reason`）。
- **设置**：profile、代理、往 `config.yaml` 追加币种。

可选币种来源：OKX `/public/instruments` 的 USDT 永续，**按 24h USDT 成交额降序**，
每条都带 `ctVal`（一张等于多少币）、`lotSz`、`minSz`、最大杠杆。
输入 `MU`、`MUUSDT`、`mu-usdt-swap` 都会归一成 `MU-USDT-SWAP`。

两个 API 现实，写死在 `console_api.instruments` 里：

- `/public/instruments` 最多返回 500 条，而永续合约不止 500 个 ——
  MU 就不在列表里。所以 `config.yaml` 里配置过的币种会单独补查并合并进来。
- SWAP 的 `/market/tickers` 只给 `vol24h`（张数）和 `volCcy24h`（基础币数量），
  **没有 USDT 成交额字段**。成交额是 `volCcy24h × last` 自己算的。
  直接拿 `volCcy24h` 排名会把 SATS / PEPE 这种单价极小的币排到 BTC 前面。

`/api/spec?symbol=MU` 返回单个合约的完整规格（含 `describe` 一行中文摘要）。

代理默认取 Windows 系统代理，在右上角或设置页改，存到 `config/console.json`。
ccxt 不读环境变量代理，必须显式传。

## 合约规格（`instruments.py`）

**一张合约不等于一个币**，而且每个合约都不一样：

| 合约 | ctVal | 1 张 = | 最大杠杆 |
|---|---|---|---|
| BTC-USDT-SWAP | 0.01 | 0.01 BTC | 100x |
| AAVE-USDT-SWAP | 0.1 | 0.1 AAVE | 50x |
| MU-USDT-SWAP | 1 | 1 MU（股票代币） | 50x |

`contracts × price` 在 BTC 上会把仓位放大 100 倍。所有"张数 ↔ 金额"的换算都走 `instruments.py`，
它同时管 `lotSz`（数量步长，不是 `minSz`）、`minSz`、`tickSz`、`maxMktSz` 和杠杆上限。
下单前 `Broker` 会读一次规格并缓存；读不到时回退到 ccxt 的市场缓存。

来自 OKX 官方 agent-skills（`okx-cex-trade` / `okx-cex-market`）的要点已经核对过：
其中"股票代币最大 5x"和"只在美股时段交易"两条，与本机实测不符 ——
MU-USDT-SWAP 的 `lever` 字段是 50，且 5 天 1439 根 5m K 线里只有 1 根没有成交。
**以交易所返回值为准。**

## 限速（`http.py`）

OKX 公共行情是 **20 请求 / 2 秒 / IP**。所有 REST 调用共用 `http.py` 里的一个滑动窗口节流器 ——
回测分页和控制台各睡各的礼貌间隔，加起来照样会超限，所以预算必须由一处统一持有。

`http.py` 同时区分三类失败：连不上（network）、HTTP 状态码（http，如 404 币种不存在）、
业务 code（api，如 51001）。以前三者一律报"连不上 OKX，检查代理"，
把币种写错误导成代理坏了。

## 回归测试

```bat
set PYTHONPATH=src
.venv\Scripts\python.exe tests\test_indicators.py
.venv\Scripts\python.exe tests\test_strategy.py
.venv\Scripts\python.exe tests\test_executor.py
.venv\Scripts\python.exe tests\test_console.py
.venv\Scripts\python.exe tests\test_instruments.py
```

- `test_indicators.py`：冻结了一份重构前的指标实现作为参照基准。
  改动 `indicators.py` 后必须重跑，确保 live 的实盘路径没有被误改。
- `test_strategy.py`：信号是 EVENT 不是 STATE；下一根开盘成交；止损保守判定；不许偷看未来。
- `test_executor.py`：对账失败拒绝交易、demo 凭据门禁、config 兼容性与非法值拒绝。
- `test_console.py`：币种归一、config 单行改写保留注释、代理优先级、HTTP 路由（离线）。
- `test_instruments.py`：ctVal 换算、`lotSz` 与 `minSz` 的区别、浮点不漂移、
  杠杆上限、`maxMktSz`、ccxt 符号映射、**回撤的单位**、限速器记账。

全部离线，不联网、不需要 API Key。

## K 线导出与画图（`fetch_mu_candles.py`）

按标的 / 周期 / 日期导出 OKX 永续 K 线，存 CSV 并画 K 线图（红涨绿跌，带成交量）。

```bat
.venv\Scripts\python.exe fetch_mu_candles.py                     :: 昨天 MU-USDT-SWAP 5m
.venv\Scripts\python.exe fetch_mu_candles.py --date 2026-10-02   :: 指定日期
.venv\Scripts\python.exe fetch_mu_candles.py --inst BTC-USDT-SWAP --bar 15m
.venv\Scripts\python.exe fetch_mu_candles.py --proxy http://127.0.0.1:7890
```

也可以双击 `export-mu-candles.bat`。产物写到 `data/`：一份 `CSV` + 一张 `PNG`，画完自动打开。

- 用 `/api/v5/market/history-candles` 的 `after` 游标翻页凑齐一整天（5m 一天 288 根，需翻 3 页）。
- 日期默认「昨天」，时区默认 `Asia/Shanghai`，用 `--date` / `--tz` 改。
- **代理自动探测**：`--proxy` > 环境变量 > `.mcp.json` > 常见本地端口 > 直连；都失败就显式 `--proxy` 指定。

## 可选依赖（画图）

只影响上一节的画图功能，没装的话脚本会自动降级成「只导 CSV」：

```bat
.venv\Scripts\python.exe -m pip install matplotlib mplfinance
```

## 已知限制

已经有的：`Ledger` 持仓账本（`state.yaml`，首次真实持仓时生成）、每轮对账
（`reconciled N symbol(s)`）、开仓时把止损写进同一请求、止损没挂上就自动平掉、
demo 门禁、回测与实盘共用策略、限速、合约规格换算。

**还没有的：**

- **回撤熔断**。仓位按 `equity_pct × leverage` 算，但没有"账户回撤到 X% 就停机"的总闸。
  目前唯一的保护是单笔 3% 止损。MU 30 天回测显示 ruin leverage 8.99x 而配置是 10x ——
  也就是说这段行情按现在的设置会把账户打穿，而系统不会自己停下来。
- **FSM 状态机**。现在是无状态的：每轮看指标 + 看账本决定动作，没有
  "连续亏损 N 次进入冷却"之类跨周期的状态。
- **行情缓存落盘**。每次启动重新拉全量历史，断线重连靠重新预热。
- **成本模型校准**。默认的 5 bps 手续费 + 3 bps 滑点是估算值，没有用真实成交回填验证过。

下一步的建议顺序：先做回撤熔断（这是唯一会导致"系统自己不会停"的缺口），
再考虑别的。

历史运行日志已清理，`logs/` 保留为空目录，程序跑起来会自动重新写入。
清理掉的东西（含 `smoke_order.py` DEMO 下单冒烟测试）在 `.cleanup-backup/<时间戳>/` 有完整备份，需要可原样取回。
