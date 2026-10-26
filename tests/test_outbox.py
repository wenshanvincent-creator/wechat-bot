import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))

import outbox
from control_store import ControlStore


def store(tmp_path):
    control = ControlStore(tmp_path)
    control.db.execute("INSERT INTO records VALUES ('channels','ch1',?,?)",
                       (json.dumps({'id': 'ch1', 'name': '测试好友', 'native_id': 'wxid_x', 'kind': 'friend',
                                    'session_id': 's1', 'enabled': True, 'allow_send': True}), time.time()))
    control.db.commit()
    return control


def test_queue_replaces_older_reply_for_same_chat(tmp_path):
    control = store(tmp_path)
    channel = control.get('channels', 'ch1')
    outbox.queue(control, channel, ['第一条'])
    outbox.queue(control, channel, ['第二条'])
    rows = outbox.due(control, time.time() + 100)
    assert len(rows) == 1
    assert json.loads(rows[0]['parts']) == ['第二条']


def test_deliver_marks_sent(tmp_path):
    control = store(tmp_path)
    outbox.queue(control, control.get('channels', 'ch1'), ['补一句'])
    sent = []
    done = outbox.deliver(control, lambda ch, text, op, deadline: sent.append(text) or {'status': 'sent'},
                          now=time.time() + 100)
    assert sent == ['补一句'] and len(done) == 1
    assert outbox.due(control, time.time() + 200) == []


def test_three_failures_stop_retrying(tmp_path):
    control = store(tmp_path)
    outbox.queue(control, control.get('channels', 'ch1'), ['发不出去'])
    def boom(*args):
        return {'status': 'failed', 'error': '没有窗口'}
    for i in range(3):
        outbox.deliver(control, boom, now=time.time() + 100 + i * 200)
    assert outbox.due(control, time.time() + 5000) == []
