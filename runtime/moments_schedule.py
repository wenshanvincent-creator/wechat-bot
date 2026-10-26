"""Optional persona-only Moments opportunities, without private chat material."""
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from moments import Moments
from proactive import quiet


def schedule(control,now):
    settings=control.meta('settings',{})
    if not settings.get('enabled') or not settings.get('moments_enabled') or not settings.get('moments_auto_publish'):return
    interval=settings.get('moments_interval_minutes',360)*60
    if quiet({'quiet_start':'23:00','quiet_end':'08:00'},now,settings.get('timezone','Asia/Shanghai')):return
    for persona in control.list('personas'):
        if not persona.get('enabled') or not persona.get('moments_access'):continue
        key='moments-opportunity:'+persona['id'];last=control.meta(key)
        if last is None:control.set_meta(key,now);continue
        if now-last<interval:continue
        control.set_meta(key,now);control.enqueue('moments_think',{'persona_id':persona['id']})


def think(control,body,client,ui):
    settings=control.meta('settings',{});persona=control.get('personas',body['persona_id'])
    if not settings.get('enabled') or not settings.get('moments_enabled') or not settings.get('moments_auto_publish') or not persona.get('enabled') or not persona.get('moments_access'):raise PermissionError('此人设的自主朋友圈已关闭')
    clock=datetime.now(ZoneInfo(settings.get('timezone','Asia/Shanghai'))).isoformat()
    instruction=persona['persona']+'''\n现在有一次自主发朋友圈的机会。只写与你的人设、当下时间和普通生活感想有关的内容；不要编造现实经历，不得使用任何群聊细节、真人姓名或隐私。没什么想说的就不发。返回 JSON {"post":false} 或 {"post":true,"text":"正文"}。'''
    message,usage=client.complete([{'role':'system','content':instruction},{'role':'user','content':'当前时间：'+clock}],json_mode=True)
    value=json.loads(message['content'])
    if value.get('post') is not True:return {'message':persona['name']+' 选择暂不发朋友圈','usage':usage}
    draft=Moments(control).save(value['text'],settings.get('moments_visibility','private'),persona['id'])
    return {**Moments(control).publish(draft['id'],ui),'usage':usage,'draft_id':draft['id']}
