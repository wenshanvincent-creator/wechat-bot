import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))

import classroom
from control_store import ControlStore


def store(tmp_path):
    control = ControlStore(tmp_path)
    control.db.execute("INSERT INTO records VALUES ('channels','ch1',?,?)",
                       (json.dumps({'id': 'ch1', 'name': '班群', 'native_id': '123@chatroom', 'kind': 'group',
                                    'session_id': 's1', 'enabled': True, 'allow_send': True}), time.time()))
    control.db.commit()
    return control


def test_parse_when_accepts_iso_and_local():
    first = classroom.parse_when('2026-10-30 15:00')
    second = classroom.parse_when('2026-10-30T15:00:00+08:00')
    assert first == second
    assert classroom.local(first) == '2026-10-30 15:00'
    assert classroom.parse_when('') is None


def test_notice_save_update_keeps_previous_time(tmp_path):
    control = store(tmp_path)
    notice = classroom.save_notice(control, {'title': '周五集合', 'when': '2026-10-30 15:00',
                                             'location': '实验楼', 'materials': '校园卡'})
    assert notice['status'] == 'active'
    updated = classroom.save_notice(control, {'when': '2026-10-30 15:30'}, notice['id'])
    assert updated['when_text'] == '2026-10-30 15:30'
    assert updated['location'] == '实验楼'
    assert updated['previous'][0]['when_text'] == '2026-10-30 15:00'
    assert classroom.list_notices(control, status='active')[0]['id'] == notice['id']


def test_reminder_rejects_past_time(tmp_path):
    control = store(tmp_path)
    try:
        classroom.add_reminder(control, 'ch1', '交表', time.time() - 60)
    except ValueError:
        pass
    else:
        raise AssertionError('past reminder should be rejected')


def test_due_reminder_is_sent_once_and_recorded(tmp_path):
    control = store(tmp_path)
    item = classroom.add_reminder(control, 'ch1', '八点交报名表', time.time() + 30)
    sent = []
    receipt = classroom.deliver(control, lambda channel, text, operation, deadline: sent.append((channel['name'], text)) or {'status': 'sent', 'message_id': 'm1'},
                                now=item['run_at'] + 1)
    assert sent == [('班群', '八点交报名表')]
    assert receipt[0]['status'] == 'sent'
    assert classroom.deliver(control, lambda *args: {'status': 'sent'}, now=item['run_at'] + 5) == []


def test_daily_reminder_reschedules_after_send(tmp_path):
    control = store(tmp_path)
    item = classroom.add_reminder(control, 'ch1', '每天交作业', time.time() + 30, repeat='daily')
    classroom.deliver(control, lambda *args: {'status': 'sent'}, now=item['run_at'] + 1)
    saved = classroom.get_reminder(control, item['id'])
    assert saved['status'] == 'scheduled'
    assert saved['run_at'] == item['run_at'] + 86400


def test_cancel_stops_future_delivery(tmp_path):
    control = store(tmp_path)
    item = classroom.add_reminder(control, 'ch1', '取消我', time.time() + 30)
    classroom.cancel_reminder(control, item['id'])
    assert classroom.deliver(control, lambda *args: {'status': 'sent'}, now=item['run_at'] + 1) == []
