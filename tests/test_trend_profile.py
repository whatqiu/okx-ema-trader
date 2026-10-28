"""Regression tests for continuing trends, deferred entries and exit separation."""
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / 'src', ROOT / 'platform/backend', ROOT / 'tests'):
    sys.path.insert(0, str(path))

import autotrader
import deps
import paper
from okx_ema_trader.config import (PaperStrategyConfig, load_config, validate,
                                  paper_strategy_for, paper_liquidation_buffer)
from okx_ema_trader.storage import Store
from okx_ema_trader.strategy import (classify_trend, classify, trend_reversed,
                                    evaluate_wave, profit_trailing_exit)
from okx_ema_trader.backtest import simulate
from test_platform_autotrader import seed_candles, INST, STEP5


def indicators(side='long'):
    a, b = (103, 100) if side == 'long' else (97, 100)
    return ({'close':a,'ema_fast':a,'ema_slow':b,'prev_ema_fast':a,'prev_ema_slow':b},
            {'close':a,'ema_fast':a,'ema_slow':b,'adx':25})


@pytest.mark.parametrize('side', ['long','short'])
def test_continuing_trend_remains_a_signal_without_another_cross(side):
    five, fifteen = indicators(side)
    assert classify(five, fifteen, 20, .02)[0] is None
    assert classify_trend(five, fifteen, 20, .02)[0].side == side
    assert classify_trend(five, fifteen, 20, .02)[0].side == side


def test_filter_recovery_allows_deferred_entry():
    five, fifteen = indicators()
    fifteen['close'] = fifteen['ema_fast'] * 1.03
    assert classify_trend(five, fifteen, 20, .02)[1] == 'deviation_too_large'
    fifteen['close'] = fifteen['ema_fast'] * 1.01
    assert classify_trend(five, fifteen, 20, .02)[0].side == 'long'


def test_exit_is_independent_of_entry_filters_and_needs_both_timeframes():
    five, fifteen = indicators()
    fifteen.update(adx=10, close=120)
    assert classify_trend(five, fifteen, 20, .02)[0] is None
    assert trend_reversed('short', five, fifteen)
    fifteen['ema_fast'] = 95
    assert not trend_reversed('short', five, fifteen)


def frames(stop=False):
    five = pd.DataFrame([dict(ts=i*300000,open=104,high=105,low=99 if stop and i==2 else 103,
                             close=104,ema_fast=103,ema_slow=100,adx=25) for i in range(12)])
    fifteen = pd.DataFrame([dict(ts=i*900000,close=104,ema_fast=103,ema_slow=100,adx=25)
                            for i in range(-3,5)])
    return five, fifteen


def test_trend_fills_next_open_and_does_not_pyramid():
    five, fifteen = frames()
    report = simulate(five,fifteen,20,.02,8,300000,0,entry_mode='trend')
    assert len(report.trades) == 1
    assert report.trades[0].entry_exec_ts > report.trades[0].entry_signal_ts
    assert report.turnover == 2


def test_stopped_trend_does_not_reenter_the_same_regime():
    five, fifteen = frames(stop=True)
    report = simulate(five,fifteen,20,.02,8,300000,0,entry_mode='trend',
                      liquidation_buffer_pct=3,leverage=20,one_entry_per_trend=True)
    assert len(report.trades) == 1
    assert report.stopped_out == 1


def test_pullback_backtest_honors_the_shared_latch_and_has_no_pyramiding():
    five, fifteen = frames()
    report = simulate(five,fifteen,20,.02,8,300000,0,entry_mode='pullback')
    assert len(report.trades) == 1
    assert report.turnover == 2
    assert report.trades[0].entry_exec_ts > report.trades[0].entry_signal_ts


@pytest.fixture
def book(tmp_path, monkeypatch):
    cfg = load_config(ROOT / 'config/config.yaml')
    cfg = replace(cfg,paper_strategy=PaperStrategyConfig(entry_mode='trend',
                  exit_on_trend_reversal=True,one_entry_per_trend=True,reentry_cooldown_bars=6))
    monkeypatch.setattr(deps,'config',lambda:cfg)
    import notifications
    monkeypatch.setattr(notifications,'enqueue',lambda *args:None)
    store = Store(tmp_path / 'book.db')
    seed_candles(store)
    autotrader.set_state(store,enabled=True,symbol=INST,notional=100,leverage=5)
    yield store
    store.close()


def tick(*args):
    return {'bid':99.9,'ask':100.1,'last':100}


def test_persistent_signal_holds_one_position_and_does_not_spam(book):
    signal = lambda *args: classify_trend(*indicators(),20,.02)[0]
    first = autotrader.maybe_trade(book,INST,signal_fn=signal,ticker_fn=tick)
    assert first['opened']
    book.set_meta(f'auto_bar_ts:{INST}','0')
    second = autotrader.maybe_trade(book,INST,signal_fn=signal,ticker_fn=tick)
    assert not second['acted']
    assert len(book.open_orders()) == 1
    assert len(book.signals()) == 1


def test_trend_entry_latch_survives_a_restart_and_losing_close(book):
    signal = lambda *args: classify_trend(*indicators(),20,.02)[0]
    opened = autotrader.maybe_trade(book,INST,signal_fn=signal,ticker_fn=tick)['opened']
    paper.close_position(book,opened['order_id'],99)
    autotrader.reset_entry_rules()
    book.set_meta(f'auto_bar_ts:{INST}','0')
    second = autotrader.maybe_trade(book,INST,signal_fn=signal,ticker_fn=tick)
    assert second['why'] == 'trend already traded'
    assert not book.open_orders()


def test_stale_short_closes_even_when_new_long_is_filtered(book):
    paper.open_position(book,INST,'short',100,5,100)
    result = autotrader.maybe_trade(book,INST,signal_fn=lambda *args:None,ticker_fn=tick)
    assert result['acted']
    assert not book.open_orders()
    assert book.orders(status='closed')[0]['reason'] == 'trend-reversal'


def test_capacity_rejection_remains_in_signal_history(book):
    result = autotrader.maybe_trade(book,INST,signal_fn=lambda *args:classify_trend(*indicators(),20,.02)[0],
                                   ticker_fn=tick,allow_open=False)
    assert result['why'] == 'at position cap'
    assert book.signals()[0]['acted'] == 0
    assert '上限' in book.signals()[0]['reason']


def test_profile_only_changes_selected_simulator(book):
    cfg = load_config(ROOT / 'config/config.yaml')
    autotrader.set_state(book,strategy_profile='trend-v1',notional=1250)
    assert paper_strategy_for(cfg,book).entry_mode == 'trend'
    assert paper_liquidation_buffer(cfg,book) == 2
    assert cfg.entry_mode == 'crossover'
    assert cfg.trading.liquidation_buffer_pct == 3
    autotrader.set_state(book,strategy_profile='legacy')
    assert paper_strategy_for(cfg,book).entry_mode == 'inherit'


def test_halt_still_allows_trend_exit_but_never_a_new_position(book):
    import health
    paper.open_position(book,INST,'short',100,5,100)
    health.set_risk_halt(book,'test halt')
    out = autotrader.maybe_trade(book,INST,signal_fn=lambda *args:classify_trend(*indicators(),20,.02)[0],ticker_fn=tick)
    assert out['acted']
    assert not book.open_orders()


def test_a_reversal_frees_capacity_before_opening_the_new_side(book):
    paper.open_position(book,INST,'short',100,5,100)
    out = autotrader.maybe_trade(book,INST,signal_fn=lambda *args:classify_trend(*indicators(),20,.02)[0],
                                  ticker_fn=tick,allow_open=False)
    assert out['acted']
    assert len(book.open_orders()) == 1
    assert book.open_orders()[0]['side'] == 'long'


def test_account_reset_clears_consumed_trend(book):
    autotrader.maybe_trade(book,INST,signal_fn=lambda *args:classify_trend(*indicators(),20,.02)[0],ticker_fn=tick)
    assert book.get_meta(f'auto_trend_entered:{INST}')
    paper.reset_account(book)
    assert book.get_meta(f'auto_trend_entered:{INST}') is None


def test_risk_exit_reason_is_saved(book):
    import health
    paper.open_position(book,INST,'long',100,5,100)
    result = health.enforce_risk(book,limits=health.RiskLimits(),marks={INST:79})
    assert result['closed']
    assert book.orders(status='closed')[0]['reason'].startswith('liquidation-risk:')


def test_risk_and_auto_use_the_same_profile_buffer(book):
    import main
    autotrader.set_state(book,strategy_profile='trend-v1')
    assert main._paper_risk_limits(book).liq_buffer_pct == 2
    assert autotrader.get_state(book)['strategy']['liquidation_buffer_pct'] == 2


@pytest.mark.parametrize('side',['long','short'])
def test_wave_reentry_needs_a_pullback_and_close_with_the_trend(side):
    five, fifteen = indicators(side)
    five.update(low=103.4 if side=='long' else 96, high=104 if side=='long' else 96.6)
    assert evaluate_wave(five,fifteen,20,.02) is None
    five['low' if side=='long' else 'high'] = five['ema_fast']
    assert evaluate_wave(five,fifteen,20,.02).side == side
    five['close'] = five['ema_fast'] * (.99 if side=='long' else 1.01)
    assert evaluate_wave(five,fifteen,20,.02) is None


@pytest.mark.parametrize('side,peak,close',[('long',104,102.9),('short',96,97.1)])
def test_profit_protection_uses_previous_closed_extreme(side,peak,close):
    updated, exit_ = profit_trailing_exit(side,100,peak,close,1.5,1)
    assert updated == peak
    assert exit_


def test_profit_protection_does_not_activate_on_an_ordinary_initial_pullback():
    assert not profit_trailing_exit('long',100,101,99,1.5,1)[1]
    assert not profit_trailing_exit('short',100,99,101,1.5,1)[1]


def test_wave_profile_counts_first_entry_and_caps_second_entry(book):
    autotrader.set_state(book,strategy_profile='wave-v1')
    sig = lambda *args:classify_trend(*indicators(),20,.02)[0]
    first = autotrader.maybe_trade(book,INST,signal_fn=sig,ticker_fn=tick)
    assert first['opened']
    paper.close_position(book,first['opened']['order_id'],101)
    with book._connect() as conn:
        conn.execute('update orders set closed_at=0')
    book.set_meta(f'auto_bar_ts:{INST}','0')
    second = autotrader.maybe_trade(book,INST,signal_fn=sig,ticker_fn=tick)
    assert second['opened']
    paper.close_position(book,second['opened']['order_id'],101)
    book.set_meta(f'auto_bar_ts:{INST}','0')
    third = autotrader.maybe_trade(book,INST,signal_fn=sig,ticker_fn=tick)
    assert third['why'] == 'trend entry limit'
    assert not book.open_orders()


def test_wave_cooldown_also_applies_after_a_winning_close(book):
    autotrader.set_state(book,strategy_profile='wave-v1')
    sig = lambda *args:classify_trend(*indicators(),20,.02)[0]
    first = autotrader.maybe_trade(book,INST,signal_fn=sig,ticker_fn=tick)
    paper.close_position(book,first['opened']['order_id'],101)
    book.set_meta(f'auto_bar_ts:{INST}','0')
    assert autotrader.maybe_trade(book,INST,signal_fn=sig,ticker_fn=tick)['why'] == 'reentry cooldown'


def test_wave_cooldown_blocks_an_immediate_opposite_entry_after_reversal(book):
    autotrader.set_state(book,strategy_profile='wave-v1')
    paper.open_position(book,INST,'short',100,5,100)
    sig = lambda *args:classify_trend(*indicators(),20,.02)[0]
    out = autotrader.maybe_trade(book,INST,signal_fn=sig,ticker_fn=tick)
    assert out['acted']
    assert out['why'] == 'reentry cooldown'
    assert not book.open_orders()


def test_wave_profit_peak_survives_process_rule_reset(book):
    autotrader.set_state(book,strategy_profile='wave-v1')
    opened = paper.open_position(book,INST,'long',100,5,100)
    autotrader.maybe_trade(book,INST,signal_fn=lambda *args:None,ticker_fn=tick)
    key = f'auto_profit_peak:{opened["order_id"]}'
    peak = float(book.get_meta(key))
    assert peak > 103
    autotrader.reset_entry_rules()
    seed_candles(book,price=97)
    book.set_meta(f'auto_bar_ts:{INST}','0')
    result = autotrader.maybe_trade(book,INST,signal_fn=lambda *args:None,
                    ticker_fn=lambda *args:{'last':103.3,'bid':103.2,'ask':103.4})
    assert result['acted']
    assert book.latest_closed_order(INST)['reason'] == 'profit-trail'
    assert not book.open_orders()


def test_wave_backtest_queues_profit_exit_and_respects_entry_cap():
    five, fifteen = frames()
    five = five.astype({k:float for k in ('open','close','high','low')})
    for i, price in enumerate([104,104,104,106,108,106.5,106,106,106,106,106,106]):
        five.loc[i,['open','close','high','low']] = [price,price,price+.1,price-.1]
    report = simulate(five,fifteen,20,.02,8,300000,0,entry_mode='wave',
                      max_entries_per_trend=2,reentry_cooldown_bars=6,
                      profit_trail_activation_pct=1.5,profit_trail_distance_pct=1)
    assert len(report.trades) == 1
    assert report.trades[0].exit_reason == 'profit-trail'
    assert report.trades[0].exit_exec_ts > report.trades[0].exit_signal_ts


@pytest.mark.parametrize('value',[float('nan'),float('inf'),0,-1,100])
def test_bad_paper_buffer_rejected(value):
    cfg = load_config(ROOT / 'config/config.yaml')
    with pytest.raises(ValueError):
        validate(replace(cfg,paper_strategy=PaperStrategyConfig(liquidation_buffer_pct=value)))
