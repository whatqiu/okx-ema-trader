from __future__ import annotations

import copy
import logging
import os
import socket
import threading
import time
from dataclasses import replace

import deps
import learning
from okx_ema_trader.broker import Broker, BrokerError, to_ccxt_symbol
from okx_ema_trader.config import load_config
from okx_ema_trader.credentials import load_credentials, default_config_path
from okx_ema_trader.executor import Executor, seconds_until_next_bar, exposure_for
from okx_ema_trader.state import Ledger


class LiveExecution:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.executor = None
        self.owner = None
        self.view = dict(mode='paper', running=False, enabled=False, error='',
                         uid='', equity=None, positions=[], updated_at=None,
                         strategy='EMA crossover · 5m/15m', symbols=[])

    def snapshot(self):
        return copy.deepcopy(self.view)

    def _claim(self):
        owner = socket.socket()
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            owner.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            owner.bind(('127.0.0.1', 18789))
            owner.listen(1)
        except OSError:
            owner.close()
            raise ValueError('已有本地实盘执行器占用 18789，不能重复启动')
        self.owner = owner

    def _inventory(self):
        executor = self.executor
        if executor.ledger.pending_entry:
            raise ValueError('实盘入场请求尚未核对，禁止切换或继续自动交易')
        exchange = executor.broker.exchange
        actual = [row for row in exchange.fetch_positions()
                  if abs(float(row.get('contracts') or 0)) > 0]
        expected = {to_ccxt_symbol(symbol): executor.ledger.get(symbol)
                    for symbol in executor.ledger.open_symbols()}
        for row in actual:
            booked = expected.get(row['symbol'])
            if (booked is None or row.get('side') != booked.side
                    or abs(float(row['contracts']) - booked.size) > max(booked.size * 1e-8, 1e-10)):
                raise ValueError('交易所持仓与独立实盘账本不一致，已阻止自动交易，请人工核对')
        if exchange.fetch_open_orders():
            raise ValueError('账户有普通未成交委托，请先核对，实盘执行器不接管未知订单')
        known_stops = {position.stop_order_id for position in expected.values()}
        for kind in ('conditional', 'oco', 'trigger', 'move_order_stop'):
            for order in exchange.fetch_open_orders(params={'ordType': kind}):
                if str(order.get('id') or '') not in known_stops:
                    raise ValueError('账户存在未知策略委托，禁止接管或自动撤单，请先在 OKX 核对')
        for symbol in executor.ledger.open_symbols():
            booked = executor.ledger.get(symbol)
            if any(row['symbol'] == to_ccxt_symbol(symbol) for row in actual):
                stop_id = executor.broker._find_stop_id(symbol, booked.entry_order_id)
                if not stop_id or stop_id != booked.stop_order_id:
                    raise ValueError(f'{symbol} 交易所保护单与账本不一致，暂停自动交易')
        return actual

    def activate(self, start_entries=True):
        with self.lock:
            if self.thread and self.thread.is_alive():
                if self.view['error']:
                    raise ValueError('执行器异常，需先暂停并核对账户')
                self.executor.allow_entries = True
                self.view['enabled'] = True
                return self.snapshot()
            if self.owner is None:
                self._claim()
            previous = self.executor
            previous_view = self.snapshot()
            try:
                config = load_config(deps.PROJECT_ROOT / 'config/pilot.yaml')
                store = deps.store()
                ledger = Ledger.load(deps.PROJECT_ROOT / 'data/live-state.yaml')
                if ledger.open_symbols() and ledger.strategy_release:
                    config = learning.apply(config, ledger.strategy_release['params'])
                elif not ledger.open_symbols():
                    config = learning.released(config, store)
                if (config.trading.exit_mode != 'fixed' or config.trading.sizing_mode != 'risk'
                        or not 0 < config.trading.risk_per_trade_pct <= 0.25
                        or not 0 < config.trading.max_portfolio_risk_pct <= 0.75
                        or not 0 < config.trading.max_same_side_risk_pct <= 0.5
                        or config.trading.leverage > 3 or config.trading.execution_cost_bps < 20
                        or config.trading.position_mode != 'cross'
                        or not config.risk.max_drawdown_pct
                        or not 0 < config.risk.max_drawdown_pct <= 5
                        or not config.risk.max_consecutive_losses):
                    raise ValueError('实盘配置必须满足 pilot 风险上限和固定止损要求')
                credentials = load_credentials(os.environ.get('OKX_LIVE_PROFILE', 'live'),
                                               default_config_path(), demo=False)
                broker = Broker(credentials, proxy=deps.proxy(), live=True)
                uid = broker.verify_live_account()
                bound_uid = store.get_meta('live_account_uid', '')
                if bound_uid and uid != bound_uid:
                    raise ValueError('实盘 API 账户与已绑定账户不一致，禁止复用账本')
                if ledger.pending_entry:
                    raise ValueError('有未完成核对的实盘入场意图，请按 data/live-state.yaml 的 client_id 到 OKX 核对，禁止自动重下')
                if set(ledger.open_symbols()) - set(config.trading.symbols):
                    raise ValueError('实盘账本含配置以外持仓，不能移除仍有持仓的标的')
                if ledger.risk.halted:
                    raise ValueError('实盘风控已熔断，请先核对 data/live-state.yaml，不能通过切换解除')
                executor = Executor(replace(config, trading=replace(config.trading, enabled=True)),
                                    broker, ledger, proxy=deps.proxy())
                self.executor = executor
                self._inventory()
                executor.reconcile()
                candidate = min(exposure_for(config.trading, executor._equity),
                                config.trading.max_total_notional)
                for symbol in config.trading.symbols:
                    broker.contracts_for_notional(symbol, candidate, broker.price(symbol))
                executor.allow_entries = start_entries
                params = learning.payload(config)
                champion = learning.registry(store).get('champion')
                if not ledger.open_symbols() or not ledger.strategy_release:
                    version = champion['version'] if champion and champion['params'] == params else learning.identity(params)
                    if ledger.strategy_release.get('version') != version:
                        ledger.strategy_state = {}
                    ledger.strategy_release = dict(params=params, version=version)
                ledger.save()
                store.set_meta('live_account_uid', uid)
                store.set_meta('execution_mode', 'live')
                self.view.update(mode='live', uid=uid, enabled=start_entries, running=True,
                                 equity=executor._equity, error='', symbols=list(config.trading.symbols),
                                 positions=[dict(symbol=symbol, **vars(ledger.get(symbol)))
                                            for symbol in ledger.open_symbols()], updated_at=time.time(),
                                 strategy=config.paper_strategy.entry_mode if config.paper_strategy.entry_mode != 'inherit' else config.entry_mode,
                                 strategy_version=ledger.strategy_release['version'])
                self.stop.clear()
                self.thread = threading.Thread(target=self._run, daemon=True, name='live-executor')
                self.thread.start()
                return self.snapshot()
            except BaseException:
                self.stop.set()
                self.view = previous_view
                self.view.update(enabled=False, running=False)
                try:
                    deps.store().set_meta('execution_mode', previous_view['mode'])
                except Exception:
                    logging.exception('未能回写执行环境；新增交易保持停止')
                self.executor = previous
                self.owner.close()
                self.owner = None
                raise

    def _run(self):
        next_signal = 0.0
        while not self.stop.is_set():
            with self.lock:
                if self.stop.is_set():
                    break
                try:
                    self._inventory()
                    self.adopt_release()
                    evaluate = time.time() >= next_signal
                    self.executor.poll(evaluate_signals=evaluate)
                    if evaluate:
                        next_signal = time.time() + seconds_until_next_bar()
                    ledger = self.executor.ledger
                    self.view.update(equity=self.executor.broker.verify_demo(),
                                     positions=[dict(symbol=symbol, **vars(ledger.get(symbol)))
                                                for symbol in ledger.open_symbols()],
                                     updated_at=time.time(), enabled=self.executor.allow_entries
                                     and not ledger.risk.halted,
                                     error=ledger.risk.halt_reason if ledger.risk.halted else '')
                except Exception as exc:
                    logging.exception('实盘执行失败，停止新增交易，保留账本与交易所保护单')
                    self.executor.allow_entries = False
                    self.view.update(enabled=False, error=f'实盘执行失败：{type(exc).__name__}；请检查本地日志和交易所持仓')
                    break
            self.stop.wait(5)
        self.view['running'] = False

    def adopt_release(self):
        executor = self.executor
        ledger = executor.ledger
        champion = learning.registry(deps.store()).get('champion')
        if (not champion or ledger.open_symbols() or ledger.pending_entry
                or ledger.strategy_release.get('version') == champion['version']):
            return False
        updated = learning.apply(executor.config, champion['params'])
        previous = {key: value for key, value in ledger.strategy_release.items() if key != 'previous'}
        ledger.strategy_release = dict(version=champion['version'], params=champion['params'], previous=previous)
        ledger.strategy_state = {}
        ledger.save()
        executor.config = updated
        self.view.update(strategy=updated.paper_strategy.entry_mode if updated.paper_strategy.entry_mode != 'inherit' else updated.entry_mode,
                         strategy_version=champion['version'])
        return True

    def pause(self):
        with self.lock:
            if self.executor:
                self.executor.allow_entries = False
            self.view['enabled'] = False
            return self.snapshot()

    def paper(self):
        with self.lock:
            if self.view['mode'] == 'live' and self.executor is None:
                raise ValueError('尚未完成实盘账户对账，不能切回模拟')
            if self.executor:
                actual = self._inventory()
                if actual or self.executor.ledger.open_symbols():
                    raise ValueError('仍有实盘持仓或待结算记录，不能切回模拟；可暂停新开仓')
            self.stop.set()
        if self.thread:
            self.thread.join(timeout=15)
            if self.thread.is_alive():
                raise ValueError('实盘执行器尚未退出，请稍后重试')
        with self.lock:
            if self.owner:
                self.owner.close()
            self.owner = self.executor = self.thread = None
            self.view.update(mode='paper', running=False, enabled=False, positions=[], error='')
            deps.store().set_meta('execution_mode', 'paper')
            return self.snapshot()

    def restore(self):
        if deps.store().get_meta('execution_mode', 'paper') == 'live':
            self.view.update(mode='live', uid=deps.store().get_meta('live_account_uid', ''))
            try:
                self.activate(start_entries=False)
            except Exception:
                logging.exception('重启后实盘对账失败')
                self.view.update(error='重启后实盘对账失败；请核对凭证、网络与持仓，新增交易保持关闭')

    def shutdown(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=15)
        if self.owner and not (self.thread and self.thread.is_alive()):
            self.owner.close()


live = LiveExecution()
