import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'platform/backend'))

import execution
from okx_ema_trader.broker import Broker, BrokerError
from okx_ema_trader.credentials import Credentials, CredentialsError, load_credentials, load_demo_credentials
from okx_ema_trader.state import Ledger, Position
from okx_ema_trader.config import load_config
from okx_ema_trader.executor import Executor
from okx_ema_trader.strategy import Signal


def test_credentials_cannot_cross_environments(tmp_path):
    path = tmp_path / 'credentials.toml'
    path.write_text('[profiles.live]\ndemo = false\napi_key="key"\nsecret_key="secret"\npassphrase="pass"\n', encoding='utf-8')
    with pytest.raises(CredentialsError):
        load_demo_credentials('live', path)
    credentials = load_credentials('live', path, demo=False)
    assert credentials.demo is False
    with pytest.raises(BrokerError):
        Broker(credentials)
    with pytest.raises(BrokerError):
        Broker(Credentials('demo', 'key', 'secret', 'pass'), live=True)


def test_missing_explicit_live_flag_is_rejected(tmp_path):
    path = tmp_path / 'credentials.toml'
    path.write_text('[profiles.live]\napi_key="key"\nsecret_key="secret"\npassphrase="pass"\n', encoding='utf-8')
    with pytest.raises(CredentialsError, match='demo = false'):
        load_credentials('live', path, demo=False)


def test_live_broker_uses_live_environment_and_equity(monkeypatch):
    exchange = Mock(headers={})
    exchange.fetch_balance.return_value = {'USDT': {'total': 999}, 'info': {'data': [
        {'details': [{'ccy': 'USDT', 'eq': '123.4'}]}]}}
    monkeypatch.setattr('okx_ema_trader.broker.ccxt.okx', lambda config: exchange)
    broker = Broker(Credentials('live', 'key', 'secret', 'pass', demo=False), live=True)
    exchange.set_sandbox_mode.assert_called_once_with(False)
    assert broker.verify_demo() == pytest.approx(123.4)
    exchange.headers = {'x-simulated-trading': '1'}
    with pytest.raises(BrokerError):
        broker.verify_demo()


def runtime(positions=None, ledger=None):
    manager = execution.LiveExecution()
    exchange = Mock()
    exchange.fetch_positions.return_value = positions or []
    exchange.fetch_open_orders.return_value = []
    broker = Mock(exchange=exchange)
    manager.executor = SimpleNamespace(broker=broker, ledger=ledger or Ledger(), allow_entries=True)
    return manager


def test_unknown_exchange_position_blocks_switch():
    manager = runtime([dict(symbol='BTC/USDT:USDT', side='long', contracts=1)])
    with pytest.raises(ValueError, match='不一致'):
        manager._inventory()
    manager.executor.broker.close_position.assert_not_called()


def test_changed_position_size_blocks_execution():
    ledger = Ledger()
    ledger.set('BTC-USDT-SWAP', Position(side='long', size=1, entry_price=100,
                                      stop_price=98, stop_order_id='stop', entry_order_id='entry'))
    manager = runtime([dict(symbol='BTC/USDT:USDT', side='long', contracts=2)], ledger)
    with pytest.raises(ValueError):
        manager._inventory()


def test_missing_protection_blocks_execution():
    ledger = Ledger()
    ledger.set('BTC-USDT-SWAP', Position(side='long', size=1, entry_price=100,
                                      stop_price=98, stop_order_id='stop', entry_order_id='entry'))
    manager = runtime([dict(symbol='BTC/USDT:USDT', side='long', contracts=1)], ledger)
    manager.executor.broker._find_stop_id.return_value = None
    with pytest.raises(ValueError, match='保护单'):
        manager._inventory()


def test_unknown_algo_blocks_execution():
    manager = runtime()
    manager.executor.broker.exchange.fetch_open_orders.side_effect = lambda **kw: (
        [dict(id='unknown')] if kw.get('params') else [])
    with pytest.raises(ValueError, match='未知策略'):
        manager._inventory()


def test_pause_disables_entries_without_closing_positions():
    manager = runtime()
    manager.view.update(mode='live', enabled=True, running=True)
    result = manager.pause()
    assert not manager.executor.allow_entries
    assert not result['enabled'] and result['running']
    manager.executor.broker.close_position.assert_not_called()


def test_cannot_switch_away_with_live_positions():
    manager = runtime()
    manager.view['mode'] = 'live'
    manager.executor.ledger.set('BTC-USDT-SWAP', Position(side='long', size=1))
    with pytest.raises(ValueError):
        manager.paper()
    assert not manager.stop.is_set()


def test_preflight_failure_never_starts_worker(monkeypatch):
    manager = execution.LiveExecution()
    owner = Mock()
    def claim():
        manager.owner = owner
    monkeypatch.setattr(manager, '_claim', claim)
    monkeypatch.setattr(execution, 'load_credentials', Mock(side_effect=CredentialsError('missing')))
    with pytest.raises(CredentialsError):
        manager.activate()
    assert manager.view['mode'] == 'paper'
    assert manager.thread is None
    owner.close.assert_called_once()


def test_worker_failure_cannot_continue_trading(monkeypatch):
    manager = runtime()
    manager.view.update(mode='live', enabled=True, running=True)
    monkeypatch.setattr(manager, '_inventory', Mock(side_effect=TimeoutError()))
    manager._run()
    assert not manager.executor.allow_entries
    assert not manager.view['enabled']
    assert not manager.view['running']
    manager.executor.broker.close_position.assert_not_called()


def test_ambiguous_order_persists_intent_and_blocks_retry(tmp_path):
    config = load_config(ROOT / 'config/pilot.yaml')
    broker = Mock(live=True)
    broker.verify_demo.return_value = 10000
    broker.set_leverage.return_value = 3
    broker.market_entry.side_effect = BrokerError('timeout')
    ledger = Ledger.load(tmp_path / 'live.yaml')
    executor = Executor(config, broker, ledger)
    with pytest.raises(BrokerError):
        executor._flip('BTC-USDT-SWAP', Signal('long', 'test'), Position.flat())
    assert Ledger.load(ledger.path).pending_entry['symbol'] == 'BTC-USDT-SWAP'
    with pytest.raises(BrokerError, match='入场意图'):
        executor._flip('BTC-USDT-SWAP', Signal('long', 'test'), Position.flat())
    assert broker.market_entry.call_count == 1


@pytest.fixture
def activation(monkeypatch, tmp_path):
    config = load_config(ROOT / 'config/pilot.yaml')
    manager = execution.LiveExecution()
    data = {}
    store = SimpleNamespace(get_meta=lambda key, default='': data.get(key, default),
                            set_meta=lambda key, value: data.__setitem__(key, value))
    broker = Mock(live=True)
    broker.verify_live_account.return_value = 'account-123'
    broker.verify_demo.return_value = 10000
    broker.get_position.return_value = None
    broker.exchange.fetch_positions.return_value = []
    broker.exchange.fetch_open_orders.return_value = []
    monkeypatch.setattr(execution.deps, 'store', lambda: store)
    monkeypatch.setattr(execution.deps, 'proxy', lambda: None)
    monkeypatch.setattr(execution.deps, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(execution, 'load_config', lambda path: config)
    monkeypatch.setattr(execution, 'load_credentials', Mock(return_value=Credentials('live', '', '', '', demo=False)))
    monkeypatch.setattr(execution, 'Broker', lambda *args, **kwargs: broker)
    monkeypatch.setattr(execution.threading, 'Thread', lambda **kwargs: Mock(is_alive=lambda: False))
    monkeypatch.setattr(manager, '_claim', lambda: setattr(manager, 'owner', Mock()))
    return manager, data, broker


def test_activate_isolates_ledger_and_disables_paper(activation, tmp_path):
    manager, data, broker = activation
    result = manager.activate()
    assert result['mode'] == 'live' and result['enabled']
    assert result['uid'] == 'account-123'
    assert 'auto_enabled' not in data
    assert data['execution_mode'] == 'live'
    assert (tmp_path / 'data/live-state.yaml').exists()
    broker.market_entry.assert_not_called()
    broker.close_position.assert_not_called()


def test_uid_change_cannot_reuse_ledger(activation):
    manager, data, broker = activation
    data['live_account_uid'] = 'different-account'
    with pytest.raises(ValueError, match='账户不一致'):
        manager.activate()
    assert manager.view['mode'] == 'paper'
    assert 'auto_enabled' not in data
    broker.market_entry.assert_not_called()


def test_restart_restores_protection_but_not_new_entries(activation):
    manager, data, broker = activation
    data.update(execution_mode='live', live_account_uid='account-123')
    manager.restore()
    assert manager.view['mode'] == 'live' and manager.view['running']
    assert not manager.executor.allow_entries
    assert not manager.view['enabled']


def test_cross_origin_cannot_activate(monkeypatch):
    import asyncio
    import main
    from starlette.requests import Request
    request = Request(dict(type='http', method='POST', scheme='http',
                           path='/api/execution', query_string=b'',
                           server=('127.0.0.1', 8788), client=('127.0.0.1', 12345),
                           headers=[(b'host', b'127.0.0.1:8788'), (b'origin', b'https://example.com')]))
    called = Mock()
    response = asyncio.run(main.execution_boundary(request, called))
    assert response.status_code == 403
    called.assert_not_called()


def test_pending_intent_blocks_switch_to_paper():
    manager = runtime()
    manager.view['mode'] = 'live'
    manager.executor.ledger.pending_entry = {'client_id': 'unconfirmed'}
    with pytest.raises(ValueError, match='尚未核对'):
        manager.paper()
    assert not manager.stop.is_set()


def test_failed_worker_start_does_not_enable_execution(activation, monkeypatch):
    manager, data, broker = activation
    thread = Mock(is_alive=lambda: False)
    thread.start.side_effect = RuntimeError('cannot start')
    monkeypatch.setattr(execution.threading, 'Thread', lambda **kwargs: thread)
    with pytest.raises(RuntimeError):
        manager.activate()
    assert manager.stop.is_set()
    assert not manager.view['enabled']
    assert data['execution_mode'] == 'paper'
    broker.market_entry.assert_not_called()
