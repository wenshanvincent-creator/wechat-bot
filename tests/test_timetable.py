import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))

import timetable
from control_store import ControlStore

TZ = ZoneInfo('Asia/Shanghai')


def store(tmp_path):
    control = ControlStore(tmp_path)
    control.db.execute("INSERT INTO records VALUES ('channels','ch1',?,?)",
                       (json.dumps({'id': 'ch1', 'name': '测试好友', 'native_id': 'wxid_x', 'kind': 'friend',
                                    'session_id': 's1', 'enabled': True, 'allow_send': True}), time.time()))
    control.db.commit()
    timetable.configure(control, enabled=True, lead_minutes=5, channel_id='ch1')
    return control


def test_parse_text_accepts_times_and_periods():
    items, errors = timetable.parse_text('周一 08:00-08:40 TOK 209\n周二 第2节 物理 HL\n坏行')
    assert errors == ['坏行']
    assert [(i['day'], i['start'], i['subject']) for i in items] == [(1, '08:00', 'TOK 209'), (2, '08:45', '物理 HL')]


def test_tick_sends_once_before_class(tmp_path):
    control = store(tmp_path)
    now = time.time()
    today = datetime.fromtimestamp(now, TZ)
    start_ts = (int(now) // 60) * 60 + 300
    start = datetime.fromtimestamp(start_ts, TZ)
    timetable.replace(control, [{'id': 'e1', 'day': today.isoweekday(), 'period': 1,
                                 'start': start.strftime('%H:%M'),
                                 'end': (start + timedelta(minutes=40)).strftime('%H:%M'),
                                 'subject': 'TOK 209'}])
    sent = []
    first = timetable.tick(control, lambda ch, text, op, deadline: sent.append(text) or {'status': 'sent'}, now=now)
    second = timetable.tick(control, lambda *args: sent.append('again') or {'status': 'sent'}, now=now + 5)
    assert len(first) == 1 and len(second) == 0
    assert len(sent) == 1 and '还有5分钟上课' in sent[0]
    assert not control.db.in_transaction
