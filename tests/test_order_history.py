import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from okx_ema_trader.storage import Store


def test_recent_closes_precede_newer_entries_and_respect_filters(tmp_path):
    store = Store(tmp_path / 'history.db')
    try:
        with store._connect() as connection:
            connection.executemany(
                'INSERT INTO orders (inst_id,side,status,created_at,opened_at,closed_at) VALUES (?,?,?,?,?,?)',
                [('NVDA-USDT-SWAP', 'short', 'closed', 100, 100, 500),
                 ('TSLA-USDT-SWAP', 'short', 'closed', 200, 200, 503),
                 ('ETH-USDT-SWAP', 'long', 'closed', 300, 300, 400),
                 ('MU-USDT-SWAP', 'long', 'filled', 450, 450, None)])
        rows = store.orders(limit=2, status='closed')
        assert [row['inst_id'] for row in rows] == ['TSLA-USDT-SWAP', 'NVDA-USDT-SWAP']
        assert store.orders(limit=1)[0]['inst_id'] == 'TSLA-USDT-SWAP'
        assert len(store.orders(inst_id='NVDA-USDT-SWAP', status='closed')) == 1
        assert [row['inst_id'] for row in store.orders(status='open')] == ['MU-USDT-SWAP']
    finally:
        store.close()
