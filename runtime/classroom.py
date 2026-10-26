"""Persistent class notices and scheduled group reminders.

Notices are the class's single source of truth for arrangements (time, place,
owner, materials). Reminders are scheduled messages tied to a notice; they are
sent verbatim at the due time and their real receipt is recorded.
"""
import json
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
NOTICE_STATUS = ('active', 'superseded', 'cancelled', 'done')
REMINDER_STATUS = ('scheduled', 'sending', 'sent', 'failed', 'cancelled')


def local(ts):
    return datetime.fromtimestamp(float(ts), TZ).strftime('%Y-%m-%d %H:%M')


def parse_when(value):
    """Accept 'YYYY-MM-DD HH:MM', ISO 8601, or epoch; naive means Beijing."""
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.timestamp()
    except ValueError:
        pass
    for pattern in ('%Y-%m-%d %H:%M', '%Y/%m/%d %H:%M', '%Y-%m-%d'):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=TZ)
            return parsed.timestamp()
        except ValueError:
            continue
    raise ValueError('时间格式无法识别，请用 2026-10-30 15:00 这样的格式')


def _tables(control):
    control.db.execute('CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY,body TEXT,created REAL,updated REAL)')
    control.db.execute('CREATE TABLE IF NOT EXISTS reminders(id TEXT PRIMARY KEY,body TEXT,created REAL,updated REAL)')


def _text(value, limit=2000):
    if value is None:
        return ''
    text = str(value).strip()
    if len(text) > limit:
        raise ValueError('内容过长（最多 %d 字）' % limit)
    return text


def save_notice(control, data, notice_id=None):
    """Create a notice, or update one in place when notice_id is given."""
    _tables(control)
    old = get_notice(control, notice_id) if notice_id else None
    title = _text(data.get('title'), 120) or (old or {}).get('title', '')
    if not title:
        raise ValueError('请给出事项名称')
    when_text = data.get('when_text') if 'when_text' in data else data.get('when')
    if when_text is None:
        when_text = (old or {}).get('when_text', '')
    when_at = parse_when(when_text) if when_text else (old or {}).get('when_at')
    body = {
        'id': notice_id or uuid.uuid4().hex[:12],
        'title': title,
        'requirements': _text(data.get('requirements')),
        'audience': _text(data.get('audience'), 200),
        'when_text': _text(when_text, 200),
        'when_at': when_at,
        'location': _text(data.get('location'), 200),
        'owner': _text(data.get('owner'), 200),
        'materials': _text(data.get('materials'), 500),
        'source': _text(data.get('source'), 500),
        'status': data.get('status') if data.get('status') in NOTICE_STATUS else 'active',
        'created_at': (old or {}).get('created_at', time.time()),
        'updated_at': time.time(),
        'superseded_by': (old or {}).get('superseded_by'),
        'previous': list((old or {}).get('previous') or []),
    }
    if 'when' in data and 'when_text' not in data:
        data['when_text'] = data['when']
    for key in ('requirements', 'audience', 'when_text', 'when_at', 'location', 'owner', 'materials', 'source', 'status'):
        if key not in data and old:
            body[key] = old.get(key)
    if old and when_at != old.get('when_at'):
        body['previous'].append({'when_text': old.get('when_text'), 'when_at': old.get('when_at')})
    with control.db:
        control.db.execute('INSERT OR REPLACE INTO notices VALUES (?,?,?,?)',
                           (body['id'], json.dumps(body, ensure_ascii=False), body['created_at'], body['updated_at']))
    control.set_meta('revision', control.meta('revision', 0) + 1)
    return body


def get_notice(control, notice_id):
    _tables(control)
    row = control.db.execute('SELECT body FROM notices WHERE id=?', (notice_id,)).fetchone()
    if not row:
        raise KeyError('没有这条班级事务')
    return json.loads(row[0])


def list_notices(control, status=None, query=None):
    _tables(control)
    items = [json.loads(row[0]) for row in control.db.execute('SELECT body FROM notices ORDER BY updated DESC')]
    if status:
        items = [item for item in items if item.get('status') == status]
    if query:
        needle = str(query).strip().lower()
        items = [item for item in items if needle in json.dumps(item, ensure_ascii=False).lower()]
    return items


def update_notice(control, notice_id, **fields):
    data = {k: v for k, v in fields.items() if v is not None}
    data['title'] = data.get('title') or get_notice(control, notice_id)['title']
    return save_notice(control, data, notice_id)


def supersede(control, notice_id, by_id):
    item = get_notice(control, notice_id)
    item['status'] = 'superseded'
    item['superseded_by'] = by_id
    item['updated_at'] = time.time()
    with control.db:
        control.db.execute('UPDATE notices SET body=?,updated=? WHERE id=?',
                           (json.dumps(item, ensure_ascii=False), item['updated_at'], notice_id))
    control.set_meta('revision', control.meta('revision', 0) + 1)


def add_reminder(control, channel_id, text, when, repeat='none', notice_id=None, created_by=''):
    _tables(control)
    content = _text(text, 1000)
    from reply_delivery import humanize
    content = humanize(content)
    if not content:
        raise ValueError('提醒内容不能为空')
    if repeat not in ('none', 'daily', 'weekly'):
        raise ValueError('重复方式只支持 none / daily / weekly')
    run_at = parse_when(when)
    if run_at is None:
        raise ValueError('请给出明确的提醒时间')
    if run_at <= time.time() + 20:
        raise ValueError('提醒时间必须晚于现在至少 20 秒')
    control.get('channels', channel_id)
    item = {
        'id': uuid.uuid4().hex[:12],
        'channel_id': channel_id,
        'notice_id': notice_id or '',
        'text': content,
        'run_at': run_at,
        'repeat': repeat,
        'status': 'scheduled',
        'created_at': time.time(),
        'created_by': created_by or '',
        'last_receipt': None,
        'last_error': '',
    }
    with control.db:
        control.db.execute('INSERT INTO reminders VALUES (?,?,?,?)',
                           (item['id'], json.dumps(item, ensure_ascii=False), item['created_at'], item['created_at']))
    control.set_meta('revision', control.meta('revision', 0) + 1)
    return item


def list_reminders(control, include_finished=True):
    _tables(control)
    items = [json.loads(row[0]) for row in control.db.execute('SELECT body FROM reminders ORDER BY created DESC')]
    if not include_finished:
        items = [item for item in items if item.get('status') in ('scheduled', 'sending')]
    return items


def get_reminder(control, reminder_id):
    row = control.db.execute('SELECT body FROM reminders WHERE id=?', (reminder_id,)).fetchone()
    if not row:
        raise KeyError('没有这条提醒')
    return json.loads(row[0])


def _store(control, item):
    item['updated'] = time.time()
    with control.db:
        control.db.execute('UPDATE reminders SET body=?,updated=? WHERE id=?',
                           (json.dumps(item, ensure_ascii=False), item['updated'], item['id']))


def cancel_reminder(control, reminder_id):
    item = get_reminder(control, reminder_id)
    if item.get('status') not in ('scheduled', 'sending'):
        raise ValueError('这条提醒已经结束，不能取消')
    item['status'] = 'cancelled'
    _store(control, item)
    control.set_meta('revision', control.meta('revision', 0) + 1)
    return item


def due_reminders(control, now):
    return [item for item in list_reminders(control, include_finished=False)
            if item.get('status') == 'scheduled' and item.get('run_at', 0) <= now]


def _finish(control, item, status, receipt=None, error=''):
    item['status'] = status
    if receipt is not None:
        item['last_receipt'] = receipt
    if error:
        item['last_error'] = error
    if status == 'sent' and item.get('repeat') in ('daily', 'weekly') and not error:
        step = 86400 if item['repeat'] == 'daily' else 604800
        item['run_at'] = float(item['run_at']) + step
        item['status'] = 'scheduled'
        item['last_error'] = ''
    _store(control, item)


def recover(control):
    """A reminder caught mid-send by a restart has an unknown outcome."""
    for item in list_reminders(control):
        if item.get('status') == 'sending':
            _finish(control, item, 'failed', error='运行器重启，发送结果未确认；请核对后手动重发')


def deliver(control, sender, now=None):
    """Send every due reminder once and record its real receipt."""
    now = time.time() if now is None else now
    sent = []
    from alerts import notify
    for item in due_reminders(control, now):
        channel = control.get('channels', item['channel_id'])
        if not channel.get('enabled') or not channel.get('allow_send'):
            _finish(control, item, 'failed', error='聊天未启用或没有发送权限')
            control.activity('reminder', {'message': '提醒未发出：频道没有发送权限', 'id': item['id']})
            notify(control, sender, '老板，「' + str(channel.get('name')) + '」的提醒没发出去：频道没有发送权限',
                   source_channel=channel)
            continue
        item['status'] = 'sending'
        _store(control, item)
        try:
            receipt = sender(channel, item['text'], 'rem-' + item['id'], now + 60)
        except Exception as error:
            _finish(control, item, 'failed', error=type(error).__name__)
            control.activity('reminder', {'message': '提醒发送失败：' + type(error).__name__, 'id': item['id']})
            notify(control, sender, '老板，「' + str(channel.get('name')) + '」的提醒没发出去：' + type(error).__name__,
                   source_channel=channel)
            continue
        if receipt.get('status') == 'sent':
            _finish(control, item, 'sent', receipt=receipt)
            control.activity('reminder', {'message': '已发送提醒：' + item['text'][:60], 'id': item['id'],
                                          'channel': channel['name']})
            sent.append(item)
        else:
            _finish(control, item, 'failed', receipt=receipt,
                    error=str(receipt.get('error') or '发送结果未确认'))
            control.activity('reminder', {'message': '提醒发送失败：' + str(receipt.get('error') or '未确认'),
                                          'id': item['id']})
            notify(control, sender, '老板，「' + str(channel.get('name')) + '」的提醒没发出去：'
                   + str(receipt.get('error') or '未确认')[:100], source_channel=channel)
    return sent


def summary(control):
    """Compact text for the prompt: active notices and upcoming reminders."""
    notices = [n for n in list_notices(control) if n.get('status') == 'active']
    upcoming = [r for r in list_reminders(control, include_finished=False) if r.get('status') == 'scheduled']
    return notices, upcoming
