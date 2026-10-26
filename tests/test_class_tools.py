import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))

from agent_tools import AgentTools
from control_store import ControlStore


def setup(tmp_path, actor='wxid_monitor'):
    control = ControlStore(tmp_path)
    control.db.execute("INSERT INTO records VALUES ('channels','ch1',?,?)",
                       (json.dumps({'id': 'ch1', 'name': '班群', 'native_id': '123@chatroom', 'kind': 'group',
                                    'session_id': 's1', 'enabled': True, 'allow_send': True}), time.time()))
    control.db.commit()
    control.set_meta('monitor', {'sender_id': 'wxid_monitor', 'name': '班长'})
    persona = {'id': 'p1', 'name': '班群助手', 'tool_access': True}
    session = {'id': 's1'}
    channel = control.get('channels', 'ch1')
    return control, AgentTools(control, persona, session, channel, actor=actor)


def test_monitor_can_save_notice_and_list_it(tmp_path):
    control, tools = setup(tmp_path)
    saved = tools.execute('class_notice_save', {'title': '周五集合', 'when': '2026-10-30 15:00',
                                                'location': '实验楼', 'materials': '校园卡'})
    assert saved['ok'] and saved['id']
    listed = tools.execute('class_notice_list', {'query': ''})
    assert listed['count'] == 1
    assert listed['notices'][0]['location'] == '实验楼'
    assert listed['notices'][0]['time'] == '2026-10-30 15:00'


def test_non_monitor_cannot_write(tmp_path):
    control, tools = setup(tmp_path, actor='wxid_classmate')
    try:
        tools.execute('class_notice_save', {'title': '假通知'})
    except PermissionError:
        pass
    else:
        raise AssertionError('students must not write official notices')
    assert tools.execute('class_notice_list', {})['count'] == 0


def test_monitor_creates_reminder_with_real_run_at(tmp_path):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    control, tools = setup(tmp_path)
    when = (datetime.now(ZoneInfo('Asia/Shanghai')) + timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
    created = tools.execute('class_reminder_create', {'text': '八点交报名表', 'when': when})
    assert created['ok'] and created['run_at'] == when
    pending = tools.execute('class_reminder_list', {})['reminders']
    assert pending[0]['channel'] == '班群'
