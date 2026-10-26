"""Explicit operator-authorized initiation for friends and groups.

Groups are allowed too, but every channel carries its own interval, quiet
hours, topic and bubble cap, so a busy group cannot be flooded.
"""
from datetime import datetime
from zoneinfo import ZoneInfo


def quiet(channel,now,timezone):
    start,end=channel.get('quiet_start'),channel.get('quiet_end')
    if not start or not end or start==end:return False
    clock=datetime.fromtimestamp(now,ZoneInfo(timezone)).strftime('%H:%M')
    return start<=clock<end if start<end else clock>=start or clock<end


def schedule(control,turns,now):
    settings=control.meta('settings',{})
    if not settings.get('enabled'):return
    for channel in control.list('channels'):
        if not all(channel.get(k) for k in ('enabled','allow_send','allow_proactive')):continue
        if channel.get('kind') not in ('friend','group'):continue
        session=control.get('sessions',channel['session_id'])
        if not session.get('enabled',True) or quiet(channel,now,settings.get('timezone','Asia/Shanghai')):continue
        key='proactive:'+channel['id'];last=control.meta(key)
        # A newly enabled chat starts its first interval now, never on boot.
        if last is None:control.set_meta(key,now);continue
        if now-last<channel.get('proactive_interval_minutes',360)*60:continue
        state=turns.state(session['id'])
        if state and state['pending_at'] is not None:continue
        control.set_meta(key,now)
        turns.proactive(session['id'],channel['id'],now)
        control.set_meta('trigger:'+session['id'],{'kind':'proactive','at':now,'topic':channel.get('proactive_topic','')})
