"""Offline, read-only comparison; never opens an account or places orders.

Compares independent single-instrument simulations, not a portfolio with a
shared position cap. Reports include costs and a chronological holdout segment.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from okx_ema_trader.backtest import simulate
from okx_ema_trader.indicators import indicator_frame

TZ = timezone(timedelta(hours=8))


def timestamp(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=TZ).timestamp() * 1000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='.test-tmp/trend-comparison.json')
    parser.add_argument('--waves', action='store_true', help='Compare more frequent trend and pullback entries')
    parser.add_argument('--variants', nargs='+', help='Run only the named variants')
    args = parser.parse_args()
    source = sqlite3.connect(f'file:{ROOT / "data/platform.db"}?mode=ro', uri=True)
    db = sqlite3.connect(':memory:')
    source.backup(db)
    source.close()
    symbols = json.loads(db.execute("select value from meta where key='auto_symbols'").fetchone()[0])
    variants = {
        'crossover_buffer3': dict(entry_mode='crossover', liquidation_buffer_pct=3),
        'crossover_independent_exit': dict(entry_mode='crossover', liquidation_buffer_pct=3, exit_on_trend_reversal=True),
        'trend_once_buffer3': dict(entry_mode='trend', liquidation_buffer_pct=3, exit_on_trend_reversal=True, reentry_cooldown_bars=6, one_entry_per_trend=True),
        'trend_once_buffer2': dict(entry_mode='trend', liquidation_buffer_pct=2, exit_on_trend_reversal=True, reentry_cooldown_bars=6, one_entry_per_trend=True),
        'trend_once_buffer1.5': dict(entry_mode='trend', liquidation_buffer_pct=1.5, exit_on_trend_reversal=True, reentry_cooldown_bars=6, one_entry_per_trend=True),
    }
    if args.waves:
        variants = {
            'trend_once_buffer2': dict(entry_mode='trend', liquidation_buffer_pct=2, exit_on_trend_reversal=True, reentry_cooldown_bars=6, one_entry_per_trend=True),
            'trend_reentry_buffer2': dict(entry_mode='trend', liquidation_buffer_pct=2, exit_on_trend_reversal=True, reentry_cooldown_bars=6),
            'pullback_buffer2': dict(entry_mode='pullback', liquidation_buffer_pct=2, exit_on_trend_reversal=True, reentry_cooldown_bars=6),
            'wave_limited_buffer2': dict(entry_mode='wave', liquidation_buffer_pct=2, exit_on_trend_reversal=True,
                                        reentry_cooldown_bars=6, max_entries_per_trend=2,
                                        profit_trail_activation_pct=1.5, profit_trail_distance_pct=1.0),
        }
    if args.variants:
        unknown = set(args.variants) - variants.keys()
        if unknown:
            parser.error(f'unknown variants: {sorted(unknown)}')
        variants = {name:variants[name] for name in args.variants}
    result = {'source_max_ts':db.execute('select max(ts) from candles where confirm=1').fetchone()[0],
              'method': 'Independent 1x notional simulations. No shared account cap, funding, exchange liquidation or circuit-breaker model. Signals on closed bars, fills at next available open; gaps flagged. Crossover baseline includes the corrected 15m environment-flip trigger.', 'runs': []}
    for symbol in symbols:
        frames = {}
        for bar in ('5m', '15m'):
            rows = [list(r) for r in db.execute('select ts,open,high,low,close,vol from candles where inst_id=? and bar=? and confirm=1 order by ts', (symbol,bar))]
            frames[bar] = indicator_frame(rows, 20, 50, 14)
        for segment, start, end in (
            ('earlier', timestamp('2026-10-02'), timestamp('2026-10-15')),
            ('holdout', timestamp('2026-10-15'), timestamp('2026-10-22')),
        ):
            f5 = frames['5m'].loc[lambda x: (x.ts >= start) & (x.ts < end)].reset_index(drop=True)
            f15 = frames['15m']
            gaps = int((f5.ts.diff().dropna() != 300000).sum())
            for name, params in variants.items():
                report = simulate(f5, f15, 20, .02, 8, 300000, 0, leverage=20, **params)
                record = dict(symbol=symbol, segment=segment, variant=name,
                              bars=len(f5), gaps=gaps, trades=len(report.trades),
                              net_pct=report.net_return*100,
                              stress_net_pct=(report.gross_return-report.turnover*.0015)*100,
                              drawdown_pct=report.max_drawdown()*100,
                              stops=report.stopped_out,
                              win_rate=sum(t.gross_return > .0016 for t in report.trades)/max(1,len(report.trades)))
                result['runs'].append(record)
                print(json.dumps(record), flush=True)
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'OUTPUT {output}')


if __name__ == '__main__':
    main()
