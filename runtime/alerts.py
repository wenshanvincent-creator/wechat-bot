"""Tell the owner in private when a message could not be delivered."""
import time


def owner_channel(control):
    monitor = control.meta('monitor', {}) or {}
    sender_id = monitor.get('sender_id')
    if not sender_id:
        return None
    return next((c for c in control.list('channels')
                 if c.get('kind') == 'friend' and c.get('native_id') == sender_id
                 and c.get('enabled') and c.get('allow_send')), None)


def notify(control, sender, text, source_channel=None):
    """Send one short DM to the owner, rate-limited, never recursive."""
    channel = owner_channel(control)
    if not channel:
        return
    if source_channel and channel.get('id') == source_channel.get('id'):
        return
    key = 'alert:' + channel['id']
    last = control.meta(key, 0) or 0
    if time.time() - last < 30:
        return
    control.set_meta(key, time.time())
    try:
        sender(channel, text[:300], 'alert-' + str(int(time.time())), time.time() + 30)
        control.activity('alert', {'message': text[:120], 'channel': (source_channel or {}).get('name', '')})
    except Exception:
        pass
