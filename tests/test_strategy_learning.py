import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'platform/backend'))

import learning
import execution
from okx_ema_trader.config import load_config, PaperStrategyConfig
from okx_ema_trader.state import Ledger, Position
from okx_ema_trader.executor import Executor


def metrics(**values):
    return dict(dict(trades=40, min_symbol_trades=20, min_symbol_net=0.02,
                     net=0.04, drawdown=0.02, profit_factor=1.5), **values)


def test_gate_rejects_small_samples_losses_drawdown_and_weak_improvements():
    good = metrics()
    baseline = {**good, 'net': 0.01}
    assert learning.eligible(good, baseline, good)
    for field, value in [('trades', 29), ('min_symbol_trades', 9), ('min_symbol_net', 0),
                         ('net', 0.01), ('profit_factor', 1.19), ('drawdown', 0.051)]:
        assert not learning.eligible({**good, field: value}, baseline, good)
    assert not learning.eligible(good, baseline, {**good, 'net': -0.01})


def test_learning_cannot_modify_money_or_credentials():
    config = load_config(ROOT / 'config/pilot.yaml')
    params = learning.payload(config)
    updated = learning.apply(config, {**params, 'adx_min': 25})
    assert updated.trading is config.trading
    assert updated.risk is config.risk
    with pytest.raises(ValueError):
        learning.apply(config, {**params, 'trading': {'leverage': 20}})


@pytest.fixture
def trial_store(monkeypatch):
    data = {}
    store = SimpleNamespace(get_meta=lambda key, default='': data.get(key, default),
                            set_meta=lambda key, value: data.__setitem__(key, value))
    monkeypatch.setattr(learning.deps, 'store', lambda: store)
    return data, store


def test_future_trial_cannot_promote_before_end(trial_store, monkeypatch):
    data, store = trial_store
    data[learning.KEY] = json.dumps({'trial': {'start': 1000000, 'end': learning.DAY * 7}})
    learner = learning.Learner()
    score = Mock()
    monkeypatch.setattr(learner, 'score', score)
    learner.run(now=learning.DAY)
    state = learning.registry(store)
    assert not state.get('champion')
    score.assert_not_called()


def test_validation_uses_frozen_params_and_future_window(trial_store, monkeypatch):
    data, store = trial_store
    config = load_config(ROOT / 'config/pilot.yaml')
    params = learning.payload(config)
    candidate = {**params, 'adx_min': 25}
    begin = learning.DAY * 20
    end = begin + learning.DAY * 7
    data[learning.KEY] = json.dumps({'trial': dict(start=begin, end=end, version='frozen',
                                                 params=candidate, baseline=params)})
    learner = learning.Learner()
    def score(store, config, start, finish, multiplier=1):
        assert (start, finish) == (begin, end)
        return {**metrics(), 'net': 0.04 if config.adx_min == 25 else 0.01}
    monkeypatch.setattr(learner, 'score', score)
    learner.run(now=end)
    state = learning.registry(store)
    assert state['champion']['version'] == 'frozen'
    assert state['champion']['params'] == candidate
    assert state['trial'] is None


def test_no_data_keeps_champion(trial_store, monkeypatch):
    data, store = trial_store
    champion = {'version': 'keep', 'params': learning.payload(load_config(ROOT / 'config/pilot.yaml'))}
    data[learning.KEY] = json.dumps({'champion': champion})
    learner = learning.Learner()
    monkeypatch.setattr(learner, 'score', Mock(side_effect=ValueError('missing candles')))
    learner.run(now=learning.DAY * 30)
    assert learning.registry(store)['champion'] == champion


def test_release_applies_only_when_flat(trial_store, tmp_path):
    data, store = trial_store
    config = load_config(ROOT / 'config/pilot.yaml')
    params = {**learning.payload(config), 'adx_min': 25}
    data[learning.KEY] = json.dumps({'champion': {'version': 'new', 'params': params}})
    manager = execution.LiveExecution()
    ledger = Ledger.load(tmp_path / 'live.yaml')
    executor = Executor(config, Mock(), ledger)
    manager.executor = executor
    ledger.set('BTC-USDT-SWAP', Position(side='long', size=1))
    assert not manager.adopt_release()
    assert executor.config.adx_min == 20
    ledger.clear('BTC-USDT-SWAP')
    ledger.pending_entry = {'client_id': 'unknown'}
    assert not manager.adopt_release()
    ledger.pending_entry = {}
    assert manager.adopt_release()
    assert executor.config.adx_min == 25
    assert executor.trading is config.trading
    assert Ledger.load(ledger.path).strategy_release['version'] == 'new'


def test_wave_state_persists_and_limits_reentry(tmp_path):
    config = load_config(ROOT / 'config/pilot.yaml')
    config = replace(config, paper_strategy=PaperStrategyConfig(entry_mode='wave', max_entries_per_trend=2))
    ledger = Ledger.load(tmp_path / 'wave.yaml')
    executor = Executor(config, Mock(), ledger)
    ind5 = dict(ts=learning.DAY, close=101, low=100, ema_fast=100, ema_slow=99)
    ind15 = dict(ts=learning.DAY, close=101, ema_fast=100, ema_slow=99, adx=30)
    signal, _ = executor._profile_signal('BTC-USDT-SWAP', ind5, ind15)
    assert signal.side == 'long'
    ledger.strategy_state['BTC-USDT-SWAP']['count'] = 2
    ledger.save()
    executor.ledger = Ledger.load(ledger.path)
    ind5['ts'] += learning.STEP
    signal, reason = executor._profile_signal('BTC-USDT-SWAP', ind5, ind15)
    assert signal is None and reason == 'trend entry limit'


def test_training_freezes_candidate_before_future_validation(trial_store, monkeypatch):
    data, store = trial_store
    base = load_config(ROOT / 'config/pilot.yaml')
    source = replace(base, paper_strategy=PaperStrategyConfig(entry_mode='wave'))
    monkeypatch.setattr(learning.deps, 'config', lambda: source)
    learner = learning.Learner()
    now = 20 * learning.DAY
    seen = []
    def score(store, config, start, end, multiplier=1):
        seen.append((start, end))
        assert end <= now
        return metrics()
    monkeypatch.setattr(learner, 'score', score)
    learner.run(now=now)
    trial = learning.registry(store)['trial']
    assert len(seen) == 3
    assert trial['start'] > now
    assert trial['end'] - trial['start'] == 7 * learning.DAY
    assert trial['params']['paper_strategy']['entry_mode'] == 'wave'


def test_replay_uses_actual_indicator_and_cost_pipeline():
    import math
    config = load_config(ROOT / 'config/pilot.yaml')
    config = replace(config, trading=replace(config.trading, symbols=('ETH-USDT-SWAP',)))
    def candles(symbol, bar, limit, end_ts, only_confirmed):
        step = learning.STEP if bar == '5m' else 3 * learning.STEP
        rows = []
        for timestamp in range(0, end_ts + 1, step):
            index = timestamp / learning.STEP
            price = 100 + index * 0.02 + math.sin(index / 10)
            rows.append(dict(ts=timestamp, open=price, high=price + 0.5, low=price - 0.5,
                             close=price, vol=1))
        return rows[-limit:]
    learner = learning.Learner()
    result = learner.score(SimpleNamespace(candles=candles), config, 600 * learning.STEP, 900 * learning.STEP)
    assert math.isfinite(result['net'])
    assert result['trades'] >= 0


def test_wave_exit_runs_even_when_entry_is_filtered(tmp_path):
    config = load_config(ROOT / 'config/pilot.yaml')
    config = replace(config, paper_strategy=PaperStrategyConfig(entry_mode='wave', exit_on_trend_reversal=True))
    ledger = Ledger.load(tmp_path / 'wave.yaml')
    ledger.set('ETH-USDT-SWAP', Position(side='long', size=1, entry_price=100))
    broker = Mock(dry_run=False)
    broker.close_position.return_value = True
    broker.price.return_value = 98
    broker.closed_pnl.return_value = -1
    executor = Executor(config, broker, ledger)
    ind5 = dict(ts=learning.DAY, close=98, low=97, ema_fast=99, ema_slow=100)
    ind15 = dict(ts=learning.DAY, close=98, ema_fast=99, ema_slow=100, adx=5)
    signal, why = executor._profile_signal('ETH-USDT-SWAP', ind5, ind15)
    assert signal is None and why == 'trend-reversal'
    assert not ledger.get('ETH-USDT-SWAP').is_open
    broker.close_position.assert_called_once()
