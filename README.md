# OKX EMA Trader

EMA20/EMA50 + ADX 的趋势跟随策略，跑在 OKX 永续合约上。

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

一键启动器（根目录，双击即可）：

| 文件 | 作用 |
|---|---|
| `start-platform.bat` | 交易平台 <http://127.0.0.1:8788>（FastAPI + 前端，端口占用时直接复用已运行的实例） |
| `start-platform-desktop.bat` | 同上，但开一个原生窗口（`pythonw`，无控制台，自动选空闲端口） |

等价的命令行（`.bat` 只是包了一层，方便不看终端的用法）：

- 盯信号：`python -m okx_ema_trader.monitor --symbol AAVE-USDT-SWAP`
- 实时 WS 观察者：`python -m okx_ema_trader.main`
- 回测：`python -m okx_ema_trader.backtest --days 90`
- 自动交易 demo 盘：`python -m okx_ema_trader.executor`（先加 `--dry-run`，只观察不下单）

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
| 2 | 15m | EMA20 与 EMA50 分出方向（相等则无环境） | `no_trend_env` |
| 3 | 15m | `abs(close - EMA20) / EMA20 <= 0.02`（偏离度） | `deviation_too_large` |
| 4 | 5m | **刚刚发生**金叉 / 死叉 | `no_cross_event` |

第 4 条是事件不是状态。判定方式是拿上一根和这一根比：

```python
# 多头：上一根 fast <= slow，这一根 fast > slow  → 金叉那一瞬间
prev_fast <= prev_slow and fast > slow
```

也就是说，5m 图上 EMA20 一直在 EMA50 上方，是**没有**信号的 —— 只有它从下方穿上去的那
一根才触发。一直持有多头状态不会每根 K 线重复发信号。

偏离度过滤只作用于 15m、只在**新开仓**时生效，不影响已经持有的仓位（否则价格走远一点
就会被误当成出场信号）。

参数都在 `config/config.yaml`：`adx_min: 20`、`deviation_max: 0.02`、
`ema_fast: 20`、`ema_slow: 50`、`adx_period: 14`。判定逻辑只有一份，在
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

`equity` 是**逐根 K 线盯市**的：持仓期间的浮亏也在序列里。这一点不是细节 ——
只在成交那一刻采样（曾经的行为）会把 8638 根 K 线压成 16 个点，于是最深的那段
浮亏、也就是真正决定会不会被强平的那段，根本不出现在序列里。实测偏差：AAVE
30 天 `ruin leverage` 报 30.86x，逐 bar 盯市的真值是 23.38x；回撤 64.86% vs
82.58%。**漏掉浮亏会往"更安全"的方向错**，而这正是选杠杆时最不能错的那个数字。

只读公共行情，不需要 API Key。`--proxy` 默认取 Windows 系统代理（Internet 选项里那个）；
传 `--proxy ""` 走直连。

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
.venv\Scripts\python.exe tests\test_symbols.py
.venv\Scripts\python.exe tests\test_instruments.py
.venv\Scripts\python.exe tests\test_platform_market.py
.venv\Scripts\python.exe tests\test_platform_paper.py
.venv\Scripts\python.exe tests\test_platform_autotrader.py
.venv\Scripts\python.exe tests\test_platform_autotrader_multi.py
.venv\Scripts\python.exe tests\test_platform_health.py
.venv\Scripts\python.exe tests\test_platform_search.py
.venv\Scripts\python.exe tests\test_http_headers.py
.venv\Scripts\python.exe tests\test_platform_prune.py
.venv\Scripts\python.exe tests\test_platform_poller.py
```

- `test_indicators.py`：冻结了一份重构前的指标实现作为参照基准。
  改动 `indicators.py` 后必须重跑，确保 live 的实盘路径没有被误改。
- `test_strategy.py`：信号是 EVENT 不是 STATE；下一根开盘成交；止损保守判定；不许偷看未来。
- `test_executor.py`：对账失败拒绝交易、demo 凭据门禁、config 兼容性与非法值拒绝。
- `test_symbols.py`：币种归一——`MU`/`MUUSDT`/`mu-usdt-swap` 归一到同一个 instId、
  非 USDT 永续一律拒绝、**`SymbolError` 必须是 `ValueError` 子类**（否则交易路径的
  `(ValueError, KeyError)` 兜不住，一个坏币种会拖垮整轮扫描而不是被跳过）。
- `test_instruments.py`：ctVal 换算、`lotSz` 与 `minSz` 的区别、浮点不漂移、
  杠杆上限、`maxMktSz`、ccxt 符号映射、**回撤的单位**、限速器记账。
- `test_platform_market.py`：平台行情层——confirm 标志位索引、成形K线的更正而非重复、
  前向/后向缺口补拉、防死循环守卫、poller 容错。
- `test_platform_paper.py`：模拟账户——双边手续费、保证金检查、强平价、
  权益连续性（平仓瞬间跳变=平仓费）、重复平仓拒绝。
- `test_platform_autotrader.py`：自动交易——与回测同源的信号评估、每根K线只评估一次、
  反向先平后开、价格获取失败记录而不穿透。
- `test_platform_autotrader_multi.py`：自选列表扫描——**去重键带币种**（否则一个币的
  bar 时间戳会吞掉其他币）、持仓上限（平仓永远放行，开仓才要看额度）、单币异常
  不拖垮整轮扫描、legacy 单币种设置迁移。
- `test_platform_health.py`：熔断与连通性——账户级回撤停机、连亏计数、强平价、
  以及 health 徽标**在自动交易关闭时不得伪装成"连接正常"**。
- `test_platform_search.py`：币种搜索——只留 USDT 永续、按**成交额**（量×价）排序防低价币霸榜、
  5 分钟缓存、离线降级为本地币种清单。
- `test_platform_prune.py`：数据保留——45 天K线窗口、**持仓永不删**、
  日志表截断 keep-N、prune 幂等、VACUUM 后数据完整。
- `test_platform_poller.py`：智能轮询——K线只在 bar 边界拉取、未确认重试、
  ticker 内存热缓存 60s 节流落盘、stop() 冲刷缓存。
- `test_http_headers.py`：请求头形状——**UA 必须像真实 Chrome**。
  原来的 `Mozilla/5.0 (compatible; okx-ema-trader)` 会被 OKX 的 Cloudflare
  以 `error code: 1010`（浏览器指纹拦截）挡回 403，而浏览器和 curl 访问同一
  URL 一切正常。改 UA 时先跑这个测试，别再写回机器人自报家门的形式。

全部离线，不联网、不需要 API Key。

## 交易平台（`platform/`：Vue 3 前端 + FastAPI 后端 + SQLite 持久化）

交易所式三栏界面（行情/图表/订单回测），数据本地永久储存，OKX 只补缺口。

```bat
start-platform.bat          rem 启动 http://127.0.0.1:8788
```

- **本地优先**：K线先读 `data/platform.db`，缺口才回源 OKX（后向 `after` 翻页补历史、
  前向 `before` 翻页补停机空洞），20 req/2s 的限速预算由 `http.py` 统一掌管。
- **成形K线**（confirm=0）也入库供图表显示，但回测/信号只读 `only_confirmed=True`。
- **后台 poller**：每 5s 刷新最新K线+ticker，浏览器端只读本地库，不直接打 OKX。
- 前端改动后需 `cd platform/frontend && npm run build`（或开发模式 `npm run dev`，
  vite 代理 /api 到 8788）。API 文档在 `/docs`。

### 币种清单与归一

搜索结果只保留 **USDT 永续**，按成交额排序。两个 OKX 的 API 现实决定了这里的实现
（原先写在旧控制台里，现在归 `platform/backend`）：

- `/public/instruments` 最多返回 500 条，而永续合约不止 500 个 —— MU 就不在列表里。
  所以已配置的币种会单独补查再合并进来。
- SWAP 的 `/market/tickers` 只给 `vol24h`（张数）和 `volCcy24h`（基础币数量），
  **没有 USDT 成交额字段**。成交额是 `volCcy24h × last` 自己算的。直接拿
  `volCcy24h` 排名会把 SATS / PEPE 这种单价极小的币排到 BTC 前面。

币种归一在 `src/okx_ema_trader/symbols.py`：`MU` / `MUUSDT` / `mu-usdt-swap`
都归一成 `MU-USDT-SWAP`。**只接受 USDT 本位永续，认不出来就报错而不是猜** ——
猜错等于在另一个市场开了仓。

## K 线导出与画图（`fetch_mu_candles.py`）

按标的 / 周期 / 日期导出 OKX 永续 K 线，存 CSV 并画 K 线图（红涨绿跌，带成交量）。

```bat
.venv\Scripts\python.exe fetch_mu_candles.py                     :: 昨天 MU-USDT-SWAP 5m
.venv\Scripts\python.exe fetch_mu_candles.py --date 2026-10-02   :: 指定日期
.venv\Scripts\python.exe fetch_mu_candles.py --inst BTC-USDT-SWAP --bar 15m
.venv\Scripts\python.exe fetch_mu_candles.py --proxy http://127.0.0.1:7890
```

也可以直接跑上面的命令。产物写到 `data/`：一份 `CSV` + 一张 `PNG`，画完自动打开。

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
demo 门禁、回测与实盘共用策略、限速、合约规格换算、账户级熔断。

**两条下单路径都有账户级熔断了**（`9c35a24` 之前只有 `platform/` 有）。

| 路径 | 会真的下单吗 | 账户级熔断 | 阈值写在哪 |
|---|---|---|---|
| `executor.py` | **是**，OKX demo 盘 | 有（`risk_guard.Guard`） | `config/config.yaml` 的 `risk:` 段 |
| `platform/` 自动交易 | 否，paper account | 有（`health.enforce_risk`） | `platform/backend/main.py` 的 `RISK_LIMITS` |

两条路径的阈值**故意不一样，不是漏改**：

| | 账户回撤上限 | 连亏停手 | 单仓上限 |
|---|---|---|---|
| `executor.py` | 60% | 3 笔 | 30% 账户权益（= 3% 止损 × 10x） |
| `platform/` | 25% | 5 笔 | 60% 该仓位的保证金 |

原因是敞口根本不同：`executor` 是 10x + 100% 权益，一次正常止损就亏掉 30%
权益，25% 会在**第一次**止损时误触发；`platform` 自动交易默认 5x + 固定 100
USDT 名义，一次止损约 3 USDT，跌到 25% 已经是事故。同一个数字塞进两套敞口，
必然有一边是在噪声上刹车。（另外 `platform` 的单仓上限按"该仓位的保证金"计，
`executor` 按"账户权益"计——分母不同，别照抄。）

熔断触发后是 **halt（停手）**，不是只平仓：只平仓会让机器人立刻按同一套逻辑
再开一笔，反复摩擦手续费。halt 会持久化，重启不解除，需要人工 `--reset-halt`
或 `/api/risk/reset`。

其他还没有的：

- **按时间冷却**。`executor` 仍然是无状态的：每轮看指标 + 看账本决定动作。连亏
  计数已经有了（存在 `state.yaml` 的 `risk` 段，重启不丢），但"亏完休息 N 根
  K 线再回来"这种跨周期的时间冷却还没有。
- **行情缓存落盘**（仅 `executor`）。`platform/` 已经把 K 线存进
  `data/platform.db`、只补缺口，并且断线重连后会自动补回中间那段空洞
  （`Store.candle_gaps()` + 轮询器的 heal）；`executor` 每次启动仍重新拉全量
  历史，断线重连靠重新预热。
- **成本模型校准**。默认的 5 bps 手续费 + 3 bps 滑点是估算值，没有用真实成交回填验证过。

下一步的建议顺序：给后端路由补测试（`tests/test_platform_routes.py` 是开头，
22 个路由目前覆盖极少），再考虑把 `main.py` 按域拆成多个 router 模块。

`logs/` 是运行产物（已被 `.gitignore` 忽略），程序跑起来会自动写入。
历史清理掉的东西（含 `smoke_order.py` DEMO 下单冒烟测试）在
`.cleanup-backup/<时间戳>/` 有完整备份，需要可原样取回。
