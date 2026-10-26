import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from policy import Ledger, fresh_inbound

CONFIG = {'timezone': 'UTC'}


def test_no_daily_quota_or_cooldown_and_receipts_survive_restart(tmp_path):
    path = tmp_path / 'ledger.sqlite'
    ledger = Ledger(path)
    assert ledger.can_send('a')
    ledger.record('first', 'a', 'claimed', 10000, 'hello', CONFIG)
    ledger.finish('first', 'sent')
    assert ledger.can_send('a')
    assert ledger.can_send('b')
    ledger.db.close()
    ledger = Ledger(path)
    assert ledger.seen('first')
    assert ledger.can_send('a')
    ledger.record('second', 'a', 'sent', 10600, 'another', CONFIG)
    for i in range(100):
        ledger.record(str(i),'a','sent',10600,'message',CONFIG)
    assert ledger.can_send('a')


def test_interrupted_or_uncertain_send_requires_review_not_automatic_retry(tmp_path):
    ledger = Ledger(tmp_path / 'ledger.sqlite')
    ledger.record('input', 'a', 'checking', 10000, '', CONFIG)
    assert ledger.seen('input')
    assert ledger.can_send('a')
    ledger.claim('input', 10001, 'hello')
    assert not ledger.can_send('a')
    ledger.finish('input', 'uncertain')
    assert not ledger.can_send('a')


def test_only_fresh_other_people_text_can_trigger_a_decision():
    def message(sender, seconds, text='hello'):
        return {'sender_id': sender, 'sent_at': datetime.fromtimestamp(seconds, timezone.utc).isoformat(), 'text': text}
    valid = message('other', 9999)
    messages = [valid, message('bot', 9999), message('other', 9000),
                message('other', 10001), message(None, 9999), message('other', 9999, None)]
    assert fresh_inbound(messages, 'bot', 10000, 180) == [valid]
