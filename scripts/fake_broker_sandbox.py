"""Interactive sandbox for exploring real signals_notify/schwab_client code
paths against tests/fake_broker.py, outside the pytest suite -- for ad hoc
investigation (e.g. "why did this order end up in this state") without
writing a full test file yet, and without the safety hazard of a raw
`python -c` script: unlike a one-liner, this always stubs _post_message
itself (a raw script never goes through tests/conftest.py's autouse
Slack-suppression fixture, and a real credentialed session will otherwise
leak a real Slack message -- found live 2026-07-29).

Sets up one isolated tmp DB, one fake account ('soxl_ira'), and drops into a
Python REPL with `broker`, `node_id`, `db`, `signals_notify`, `schwab_client`,
`schwab_safety` already imported and wired -- explore interactively, then
promote whatever you found into a real tests/test_fake_broker_*.py scenario.

Usage:
  .venv/bin/python scripts/fake_broker_sandbox.py [--ticker ABC] [--account soxl_ira]
"""
import argparse
import code
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

import signals_config
import signals_db
import signals_notify
import schwab_client
import schwab_safety

from fake_broker import FakeBroker, FakeUtils


def setup(ticker: str, account: str):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    signals_config.DB_PATH = Path(tmp_db.name)
    signals_config.RESEARCH_DB_PATH = Path(tempfile.mktemp(suffix='.db'))

    state_dir = Path(tempfile.mkdtemp())
    schwab_safety.STATE_PATH = state_dir / "order_counts.json"
    schwab_safety.KILL_SWITCH_PATH = state_dir / "kill_switch.json"
    schwab_safety.TICKER_AUTOMATION_PATH = state_dir / "ticker_automation.json"
    schwab_safety.NODE_AUTOMATION_PATH = state_dir / "node_automation.json"
    schwab_safety.AUTO_FILL_DETECTION_PATH = state_dir / "auto_fill_detection.json"
    schwab_safety.NODE_AUTO_FILL_DETECTION_PATH = state_dir / "node_auto_fill_detection.json"
    schwab_safety.AUTOMATION_ENABLED_TICKERS = {ticker}
    schwab_safety._now = lambda: datetime(2026, 7, 29, 10, 30)  # inside a real signal window
    schwab_safety._open_orders = lambda acct: []

    # The actual safety fix this script exists for -- never leak a real
    # Slack post from an ad hoc investigation.
    noop = lambda text, blocks=None, thread_ts=None, reply_broadcast=False: (None, None)
    import signals_blocks
    signals_blocks._post_message = noop
    for mod in (schwab_client, signals_notify):
        if hasattr(mod, '_post_message'):
            mod._post_message = noop

    broker = FakeBroker()
    broker.account_hashes = {account: f'HASH_{account.upper()}'}
    schwab_client._client = broker
    schwab_client._account_hashes = dict(broker.account_hashes)
    schwab_client._get_client = lambda interactive=False: broker
    schwab_client.Utils = FakeUtils

    signals_db.ensure_tables()
    signals_db.add_node(ticker, 'TrailingBothZScoreBreakout', 'sandbox', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account=?, starting_notional=800 WHERE ticker=?",
                   (account, ticker))
        c.commit()
    node_id = [n for n in signals_db.get_watchlist() if n['ticker'] == ticker][0]['id']

    print(f"Sandbox ready: DB={tmp_db.name}  ticker={ticker!r}  account={account!r}  node_id={node_id}")
    print("Available names: broker, node_id, ticker, account, signals_db (as db), "
          "signals_notify, schwab_client, schwab_safety")
    return broker, node_id


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ticker', default='SANDBOX_TICKER')
    ap.add_argument('--account', default='soxl_ira')
    args = ap.parse_args()

    broker, node_id = setup(args.ticker, args.account)
    ns = dict(
        broker=broker, node_id=node_id, ticker=args.ticker, account=args.account,
        db=signals_db, signals_db=signals_db, signals_notify=signals_notify,
        schwab_client=schwab_client, schwab_safety=schwab_safety,
    )
    code.interact(banner="", local=ns, exitmsg="")


if __name__ == '__main__':
    main()
