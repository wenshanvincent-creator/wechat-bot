"""Retry replies that failed before reaching WeChat.

Only pre-send failures are queued (the message definitely did not go out),
so a retry cannot duplicate a delivered bubble. The latest reply per chat is
kept; older queued rows for the same chat are replaced.
"""
import json
import time
import uuid


def _table(control):
    control.db.execute('CREATE TABLE IF NOT EXISTS outbox('
                       'id TEXT PRIMARY KEY,channel_id TEXT,native_id TEXT,parts TEXT,'
                       'attempts INTEGER,next_at REAL,status TEXT,created REAL)')


def queue(control, channel, parts):
    texts = [part for part in parts if isinstance(part, str) and part.strip()]
    if not texts or not channel.get('id'):
        return
    _table(control)
    with control.db:
        control.db.execute("DELETE FROM outbox WHERE channel_id=? AND status='queued'", (channel['id'],))
        control.db.execute('INSERT INTO outbox VALUES (?,?,?,?,0,?,?,?)',
                           (uuid.uuid4().hex[:12], channel['id'], channel.get('native_id', ''),
                            json.dumps(texts, ensure_ascii=False), time.time() + 30, 'queued', time.time()))


def due(control, now):
    _table(control)
    return [dict(row) for row in control.db.execute(
        "SELECT * FROM outbox WHERE status='queued' AND next_at<=? ORDER BY created", (now,))]


def _update(control, key, **fields):
    sets = ','.join(f'{name}=?' for name in fields)
    with control.db:
        control.db.execute(f'UPDATE outbox SET {sets} WHERE id=?', (*fields.values(), key))


def deliver(control, sender, now=None):
    """Send each due retry once; give up after three attempts."""
    from alerts import notify
    now = time.time() if now is None else now
    done = []
    for item in due(control, now):
        try:
            channel = control.get('channels', item['channel_id'])
        except KeyError:
            _update(control, item['id'], status='failed')
            continue
        if not channel.get('enabled') or not channel.get('allow_send'):
            _update(control, item['id'], status='failed')
            continue
        texts = json.loads(item['parts'])
        ok, error = True, ''
        for index, text in enumerate(texts):
            try:
                receipt = sender(channel, text, 'retry-' + item['id'] + '-' + str(index), now + 45)
            except Exception as exc:
                ok, error = False, type(exc).__name__
                break
            if receipt.get('status') != 'sent':
                ok, error = False, str(receipt.get('error') or '未确认')
                break
        if ok:
            _update(control, item['id'], status='sent')
            control.activity('outbox', {'message': '补发成功：' + texts[0][:50], 'channel': channel['name']})
            done.append(item)
        else:
            attempts = int(item['attempts']) + 1
            if attempts >= 3:
                _update(control, item['id'], status='failed', attempts=attempts)
                notify(control, sender, '老板，「' + str(channel.get('name')) + '」有条回复补发三次都没成功：'
                       + error[:100], source_channel=channel)
            else:
                _update(control, item['id'], attempts=attempts, next_at=now + 120)
    return done
