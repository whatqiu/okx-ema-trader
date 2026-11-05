from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import asdict, replace

import deps
from okx_ema_trader.backtest import simulate
from okx_ema_trader.config import PaperStrategyConfig, paper_strategy_for, validate
from okx_ema_trader.indicators import indicator_frame

DAY = 86400000
STEP = 300000
KEY = 'strategy_learning_v1'


def payload(config):
    return dict(adx_min=config.adx_min, deviation_max=config.deviation_max,
                ema_fast=config.ema_fast, ema_slow=config.ema_slow, adx_period=config.adx_period,
                entry_mode=config.entry_mode, pullback_tolerance=config.pullback_tolerance,
                paper_strategy=asdict(config.paper_strategy))


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def apply(config, value):
    allowed = set(payload(config))
    if set(value) != allowed:
        raise ValueError('学习策略字段不匹配，禁止修改风控与账户配置')
    result = replace(config, **{key: val for key, val in value.items() if key != 'paper_strategy'},
                     paper_strategy=PaperStrategyConfig(**value['paper_strategy']))
    validate(result)
    if result.paper_strategy.entry_mode not in ('inherit', 'trend', 'wave'):
        raise ValueError('不支持该学习策略')
    return result


def registry(store):
    return json.loads(store.get_meta(KEY, '{}'))


def released(config, store):
    champion = registry(store).get('champion')
    return apply(config, champion['params']) if champion else config


def eligible(candidate, baseline, stress):
    return (candidate['trades'] >= 30 and candidate['min_symbol_trades'] >= 10
            and candidate['min_symbol_net'] > 0 and candidate['net'] > 0
            and candidate['profit_factor'] >= 1.2 and candidate['drawdown'] <= 0.05
            and candidate['drawdown'] <= baseline['drawdown'] * 1.1 + 0.001
            and candidate['net'] >= baseline['net'] + 0.002 and stress['net'] > 0)


class Learner:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None

    def start(self, stop):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, args=(stop,), daemon=True, name='strategy-learning')
        self.thread.start()

    def _loop(self, stop):
        while not stop.wait(600):
            try:
                from okx_ema_trader.config import load_config
                import market
                config = load_config(deps.PROJECT_ROOT / 'config/pilot.yaml')
                for symbol in config.trading.symbols:
                    if stop.is_set():
                        return
                    for bar in (config.bar_5m, config.bar_15m):
                        market.ensure_candles(deps.store(), symbol, bar, market.bars_for_days(bar, 24))
                self.run()
            except Exception:
                logging.exception('策略学习检查失败，维持原实盘版本')
                try:
                    store = deps.store()
                    state = registry(store)
                    state.update(enabled=True, checked_at=int(time.time() * 1000),
                                 status='学习检查或行情补齐失败，维持原实盘参数；请检查本地日志')
                    store.set_meta(KEY, json.dumps(state, ensure_ascii=False, allow_nan=False))
                except Exception:
                    logging.exception('未能记录策略学习状态')

    def frames(self, store, config, start, end):
        output = []
        for symbol in config.trading.symbols:
            frames = []
            for bar, step in ((config.bar_5m, STEP), (config.bar_15m, STEP * 3)):
                warm_start = start - max(config.ema_slow * 4, config.adx_period * 8) * step
                rows = store.candles(symbol, bar, limit=20000, end_ts=end - step, only_confirmed=True)
                rows = [row for row in rows if row['ts'] >= warm_start]
                if (not rows or rows[0]['ts'] > warm_start + step or rows[-1]['ts'] < end - step * 2
                        or any(right['ts'] - left['ts'] != step for left, right in zip(rows, rows[1:]))):
                    raise ValueError(f'{symbol} {bar} 缺少完整历史或存在缺口，保持原参数')
                frame = indicator_frame([[row[key] for key in ('ts', 'open', 'high', 'low', 'close', 'vol')]
                                         for row in rows], config.ema_fast, config.ema_slow, config.adx_period)
                frames.append(frame[frame.ts >= start] if bar == config.bar_5m else frame)
            output.append(frames)
        return output

    def score(self, store, config, start, end, multiplier=1):
        profile = config.paper_strategy
        returns, net_values, drawdowns, counts = [], [], [], []
        cost = config.trading.execution_cost_bps / 2 * multiplier
        for frame5, frame15 in self.frames(store, config, start, end):
            report = simulate(frame5, frame15, config.adx_min, config.deviation_max, cost,
                              STEP, config.trading.stop_loss_pct, entry_mode=(
                                  profile.entry_mode if profile.entry_mode != 'inherit' else config.entry_mode),
                              exit_on_trend_reversal=profile.exit_on_trend_reversal,
                              reentry_cooldown_bars=profile.reentry_cooldown_bars,
                              one_entry_per_trend=profile.one_entry_per_trend,
                              max_entries_per_trend=profile.max_entries_per_trend,
                              profit_trail_activation_pct=profile.profit_trail_activation_pct,
                              profit_trail_distance_pct=profile.profit_trail_distance_pct,
                              pullback_tolerance=config.pullback_tolerance)
            returns.extend(trade.gross_return - 2 * cost / 10000 for trade in report.trades)
            net_values.append(report.net_return)
            drawdowns.append(report.max_drawdown(1))
            counts.append(len(report.trades))
        loss = -sum(value for value in returns if value < 0)
        return dict(trades=len(returns), min_symbol_trades=min(counts),
                    min_symbol_net=min(net_values), net=sum(net_values) / len(net_values),
                    drawdown=max(drawdowns), profit_factor=(
                        sum(value for value in returns if value > 0) / loss if loss else 999.0))

    def run(self, now=None):
        if not self.lock.acquire(blocking=False):
            return
        try:
            self._run(int(now if now is not None else time.time() * 1000))
        finally:
            self.lock.release()

    def _run(self, now):
        from okx_ema_trader.config import load_config
        store = deps.store()
        state = registry(store)
        base = load_config(deps.PROJECT_ROOT / 'config/pilot.yaml')
        end = now // STEP * STEP
        state.update(enabled=True, checked_at=now)
        execution_model = dict(trading=asdict(base.trading), bar_5m=base.bar_5m, bar_15m=base.bar_15m)
        try:
            trial = state.get('trial')
            if trial:
                if now - trial['start'] > 35 * DAY:
                    state.update(trial=None, status='冻结候选已超出本地验证保留期限，重新收集未来样本', next_trial_at=now + DAY)
                elif trial.get('execution_model', execution_model) != execution_model:
                    state.update(trial=None, status='执行成本或风控配置已变化，旧候选作废，维持原参数', next_trial_at=now + DAY)
                elif end < trial['end']:
                    state['status'] = '等待冻结候选的未来 7 天验证数据'
                else:
                    candidate = apply(base, trial['params'])
                    baseline = apply(base, trial['baseline'])
                    result = self.score(store, candidate, trial['start'], trial['end'])
                    old = self.score(store, baseline, trial['start'], trial['end'])
                    stress = self.score(store, candidate, trial['start'], trial['end'], 1.5)
                    passed = eligible(result, old, stress)
                    trial.update(result=result, baseline_result=old, stress=stress, passed=passed)
                    state.setdefault('history', []).append(trial)
                    state['history'] = state['history'][-20:]
                    if passed:
                        state['previous'] = state.get('champion')
                        state['champion'] = dict(version=trial['version'], params=trial['params'],
                                                  approved_at=now, result=result)
                    state['trial'] = None
                    state['next_trial_at'] = now + DAY
                    state['status'] = '验证通过，等待实盘空仓自动应用' if passed else '验证未达标，维持原参数'
            elif now >= state.get('next_trial_at', 0):
                baseline = released(base, store)
                source = deps.config()
                profile = paper_strategy_for(source, store)
                source_params = payload(replace(source, paper_strategy=replace(profile, liquidation_buffer_pct=None)))
                original = apply(base, source_params)
                start = end - 14 * DAY
                choices = []
                for adx in (source.adx_min, min(source.adx_min + 5, 40), min(source.adx_min + 10, 40)):
                    candidate = replace(original, adx_min=adx)
                    score = self.score(store, candidate, start, end)
                    if score['net'] > 0 and score['trades'] >= 30:
                        choices.append((score['net'], candidate, score))
                if not choices:
                    state.update(status='训练期无合格候选，维持原参数', next_trial_at=now + DAY)
                else:
                    _, selected, training = max(choices, key=lambda choice: choice[0])
                    params = payload(selected)
                    if params == payload(baseline):
                        state.update(status='模拟策略与当前版本相同，无需更新', next_trial_at=now + DAY)
                    else:
                        begin = (now // STEP + 1) * STEP
                        state['trial'] = dict(version=identity(dict(params=params, start=begin)),
                                              params=params, baseline=payload(baseline), training=training,
                                              execution_model=execution_model,
                                              start=begin, end=begin + 7 * DAY)
                        state['status'] = '候选已冻结，开始收集未来 7 天验证数据'
        except ValueError as exc:
            state['status'] = str(exc)
        store.set_meta(KEY, json.dumps(state, ensure_ascii=False, allow_nan=False))


learner = Learner()
