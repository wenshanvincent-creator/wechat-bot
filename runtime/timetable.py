"""Weekly class timetable and pre-class reminders."""
import json
import re
import time
import uuid
from datetime import datetime, time as clock, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
DAYS = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '日': 7, '天': 7}
PERIODS = {
    1: ('08:00', '08:40'), 2: ('08:45', '09:25'), 3: ('09:35', '10:15'),
    4: ('10:20', '11:00'), 5: ('11:05', '11:45'), 6: ('12:45', '13:25'),
    7: ('13:30', '14:10'), 8: ('14:15', '14:55'), 9: ('15:00', '15:40'),
    10: ('15:45', '16:25'),
}
LINE = re.compile(r'^(?:周|星期)([一二三四五六日天])\s*(?:第\s*(\d{1,2})\s*节)?\s*'
                  r'(?:(\d{1,2}:\d{2})\s*[-~至到]\s*(\d{1,2}:\d{2}))?\s*(.+)$')


def _tables(control):
    control.db.execute('CREATE TABLE IF NOT EXISTS timetable(id TEXT PRIMARY KEY,body TEXT,updated REAL)')
    control.db.execute('CREATE TABLE IF NOT EXISTS timetable_sent(day TEXT,entry_id TEXT,PRIMARY KEY(day,entry_id))')


def entries(control):
    _tables(control)
    rows = [json.loads(row[0]) for row in control.db.execute('SELECT body FROM timetable')]
    return sorted(rows, key=lambda item: (item['day'], item['start']))


def settings(control):
    cfg = control.meta('timetable', {}) or {}
    return {'enabled': bool(cfg.get('enabled')), 'lead_minutes': int(cfg.get('lead_minutes', 5)),
            'channel_id': cfg.get('channel_id', '')}


def configure(control, **values):
    cfg = settings(control)
    if 'enabled' in values:
        cfg['enabled'] = bool(values['enabled'])
    if 'lead_minutes' in values:
        lead = int(values['lead_minutes'])
        if not 1 <= lead <= 120:
            raise ValueError('提前分钟数要在 1–120 之间')
        cfg['lead_minutes'] = lead
    if 'channel_id' in values and values['channel_id']:
        control.get('channels', values['channel_id'])
        cfg['channel_id'] = values['channel_id']
    control.set_meta('timetable', cfg)
    return cfg


def replace(control, items):
    _tables(control)
    now = time.time()
    with control.db:
        control.db.execute('DELETE FROM timetable')
        for item in items:
            control.db.execute('INSERT INTO timetable VALUES (?,?,?)',
                               (item['id'], json.dumps(item, ensure_ascii=False), now))
    control.set_meta('revision', control.meta('revision', 0) + 1)
    return entries(control)


def parse_text(text):
    items, errors = [], []
    for raw in str(text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        match = LINE.match(line)
        if not match:
            errors.append(line[:40])
            continue
        day, period, start, end, rest = match.groups()
        number = int(period) if period else 0
        if not start:
            if not number or number not in PERIODS:
                errors.append(line[:40])
                continue
            start, end = PERIODS[number]
        else:
            number = number or next((index for index, (a, b) in PERIODS.items() if (a, b) == (start, end)), 0)
        subject = rest.strip()
        if not subject:
            errors.append(line[:40])
            continue
        items.append({'id': uuid.uuid4().hex[:12], 'day': DAYS[day], 'period': number,
                      'start': start, 'end': end, 'subject': subject})
    return items, errors


def message(item, lead):
    period = ('第%d节 ' % item['period']) if item.get('period') else ''
    return '还有%d分钟上课：%s%s %s' % (lead, period, item['start'], item['subject'])


def _sent(control, day, entry_id):
    _tables(control)
    return control.db.execute('SELECT 1 FROM timetable_sent WHERE day=? AND entry_id=?', (day, entry_id)).fetchone() is not None


def _mark(control, day, entry_id):
    with control.db:
        control.db.execute('INSERT OR IGNORE INTO timetable_sent VALUES (?,?)', (day, entry_id))


def tick(control, sender, now=None):
    now = time.time() if now is None else now
    cfg = settings(control)
    if not cfg['enabled'] or not cfg['channel_id']:
        return []
    channel = control.get('channels', cfg['channel_id'])
    if not channel.get('enabled') or not channel.get('allow_send'):
        return []
    today = datetime.fromtimestamp(now, TZ)
    weekday = today.isoweekday()
    sent = []
    for item in entries(control):
        if item['day'] != weekday:
            continue
        try:
            start = datetime.combine(today.date(), clock.fromisoformat(item['start']), TZ).timestamp()
        except ValueError:
            continue
        fire = start - cfg['lead_minutes'] * 60
        day_key = today.date().isoformat()
        if 0 <= now - fire < 120 and not _sent(control, day_key, item['id']):
            from alerts import notify
            try:
                receipt = sender(channel, message(item, cfg['lead_minutes']), 'tt-' + item['id'], now + 60)
            except Exception as error:
                control.activity('timetable', {'message': '课前提醒发送失败：' + type(error).__name__,
                                               'subject': item['subject']})
                notify(control, sender, '老板，「' + str(channel.get('name')) + '」的课前提醒没发出去：'
                       + type(error).__name__, source_channel=channel)
                continue
            if receipt.get('status') == 'sent':
                _mark(control, day_key, item['id'])
                control.activity('timetable', {'message': '已发送课前提醒：' + item['subject'][:40],
                                               'channel': channel['name']})
                sent.append(item)
            else:
                notify(control, sender, '老板，「' + str(channel.get('name')) + '」的课前提醒没发出去：'
                       + str(receipt.get('error') or '未确认')[:100], source_channel=channel)
    with control.db:
        control.db.execute('DELETE FROM timetable_sent WHERE day<?',
                           ((today.date() - timedelta(days=14)).isoformat(),))
    return sent
