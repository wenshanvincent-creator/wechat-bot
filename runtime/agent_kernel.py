"""Single-active-persona chat engine, used by live and test adapters."""
import hashlib
import json
import re
import time
from datetime import datetime,timezone
from zoneinfo import ZoneInfo

from agent_state import AgentState,ScopedStores
from control_store import active_persona
from episodic_memory import EpisodicMemory
from model_view import GroupView
from policy import Ledger
from provider_client import ProviderClient
from reply_delivery import deliver,transport_parts

ACTION_WORDS=re.compile(r'提醒|到点|叫一下|喊一下|记一下|记下|安排一下|别忘了')
MUTE_WORDS=re.compile(r'(先)?别(说话|出声|吭声|聊)|闭嘴|安静(一下|会儿)?|禁言')
RESUME_WORDS=re.compile(r'(可以|能|重新|继续)说话|说话吧|解除(禁言|静音)|不用憋着|好了.{0,4}说')


class SessionView(GroupView):
    def __init__(self,labels=None,agent_id='',timezone='Asia/Shanghai'):
        super().__init__(labels,timezone=timezone);self.agent_id=agent_id

    def messages(self,messages):
        copies=[]
        for m in messages:
            item=dict(m)
            if item.get('agent_id')==self.agent_id:
                item['display_name']=(item.get('display_name') or '我')+'（你）'
            copies.append(item)
        return super().messages(copies)


class AgentKernel:
    def __init__(self,control,counter,sender,client_factory=ProviderClient,tool_factory=None,before_send=None):
        self.control=control;self.turns=AgentState(control);self.scopes=ScopedStores(control,counter)
        self.ledger=Ledger(control.root/'decisions.sqlite');self.sender=sender
        self.client_factory=client_factory;self.tool_factory=tool_factory
        self.before_send=before_send

    def ingest(self,channel,messages,view,now=None):
        now=time.time() if now is None else now
        channel=self.control.get('channels',channel['id'])
        if not channel.get('enabled') or not channel.get('allow_read'):return 0
        monitor=self.control.meta('monitor',{}) or {}
        if monitor.get('sender_id'):
            for message in messages:
                if message.get('sender_id')!=monitor['sender_id'] or not message.get('text'):
                    continue
                text=message['text']
                if self.control.meta('mute') and RESUME_WORDS.search(text):
                    self.control.set_meta('mute',None)
                    self.control.activity('mute',{'message':'已解除静音','channel':channel['name']})
                elif MUTE_WORDS.search(text) and not RESUME_WORDS.search(text):
                    self.control.set_meta('mute',{'at':now,'channel':channel['name'],'by':monitor['sender_id']})
                    self.control.activity('mute',{'message':'已静音，等老板让说话再开口','channel':channel['name']})
        # Keep the native wxid -> nickname table for this chat so later turns
        # address people correctly even after a rename or a restart.
        try:
            labels=getattr(view,'labels',None)
            if labels:
                fresh={k:v for k,v in labels.items() if v and k}
                if fresh:self.control.set_meta('labels:'+channel['native_id'],fresh)
        except Exception:pass
        session=self.control.get('sessions',channel['session_id'])
        conversation=self.scopes.conversation(session['id']);accepted=[]
        for message in messages:
            if not message.get('text') or self.turns.is_outgoing(message['message_id']):continue
            item={**message,'display_name':message.get('display_name') or view.name(message['sender_id']),'channel_name':channel['name']}
            conversation.append(session['id'],[item])
            age=now-datetime.fromisoformat(message['sent_at']).timestamp()
            if 0<=age<=180 and self.turns.human(session['id'],message['message_id'],channel['id'],now,self.control.meta('settings',{}).get('debounce_ms',1500)):
                accepted.append(item)
        conversation.db.close()
        archive=EpisodicMemory(self.control,active_persona(session),session['id'])
        try:archive.append(channel,[{**m,'display_name':m.get('display_name') or view.name(m['sender_id'])} for m in messages if m.get('text') and not self.turns.is_outgoing(m['message_id'])],'human')
        finally:archive.close()
        if accepted:
            self.control.set_meta('trigger:'+session['id'],{'kind':'human','at':now,'channel':channel['id']})
            for persona in [active_persona(session)]:
                memory=self.scopes.memory(persona,session['id']);memory.enqueue(session['id'],accepted);memory.db.close()
        return len(accepted)

    def recall_all(self,persona,recent,view,current):
        """A persona remembers the same things in every chat it speaks in.

        Facts stay stored per chat (that is where they were said), but recall
        reads every chat this persona is enabled for, so what it learned in one
        conversation is available in the others.
        """
        sessions=[s for s in self.control.list('sessions')
                  if persona['id'] in s.get('persona_ids',[]) and s.get('enabled',True)]
        ids=[s['id'] for s in sessions]
        if current not in ids:ids.append(current)
        names={s['id']:(s.get('name') or s['id']) for s in sessions}
        names.setdefault(current,self.control.get('sessions',current).get('name') or current)
        pieces=[];seen=set();remaining=1200
        for session_id in ids:
            memory=self.scopes.memory(persona['id'],session_id)
            try:items=memory.recall(session_id,recent)
            finally:memory.db.close()
            if not items:continue
            # Resolve speaker names with that chat's own label map, then tag the
            # source chat so cross-chat facts stay attributable.
            local=self.view(session_id,persona['id'])
            for text in local.memories(items):
                key=text.split('：',1)[-1]
                if key in seen:continue
                seen.add(key);pieces.append('['+names[session_id]+'] '+text)
        for session_id in ids:
            archive=EpisodicMemory(self.control,persona['id'],session_id)
            try:extra=archive.recall()
            finally:archive.close()
            for text in extra:
                if len(text)<=remaining:
                    pieces.append(text);remaining-=len(text)
        return pieces

    def client(self,persona):
        provider=self.control.get('providers',persona['provider_id'])
        return self.client_factory(provider,self.control.secret(provider['id']))

    def view(self,session,agent_id=''):
        conversation=self.scopes.conversation(session)
        if not conversation.initialized(session):conversation.append(session,[])
        _,_,rows=conversation.snapshot(session);conversation.db.close()
        labels={m.get('sender_id',m.get('sender')):m['display_name'] for _,m in rows if m.get('display_name') and not m.get('agent_id')}
        # Identity is always the wxid; the nickname is only how you address them.
        # Native per-chat member names win over whatever the history happened to
        # carry, so a renamed contact does not turn into a second stranger.
        channel=next((c for c in self.control.list('channels')
                      if c.get('session_id')==session and c.get('enabled')),None)
        if channel:
            stored=self.control.meta('labels:'+channel['native_id'],{})
            if stored:labels.update({k:v for k,v in stored.items() if v})
        return SessionView(labels,agent_id,self.control.meta('settings',{}).get('timezone','Asia/Shanghai'))

    def step(self,session_id,now=None):
        now=time.time() if now is None else now
        session=self.control.get('sessions',session_id)
        if self.control.meta('mute'):
            self.turns.stop(session_id);return None
        if not session.get('enabled',True) or not self.control.meta('settings',{}).get('enabled') or not self.turns.due(session,now):return None
        state=self.turns.state(session_id);epoch=state['epoch']
        trigger=self.control.meta('trigger:'+session_id,{})
        # Reply to the channel that triggered this turn, not to whatever chat
        # happened to write session_run last.
        channel=self.control.get('channels',trigger.get('channel') or state['channel_id'])
        if not channel.get('enabled') or not channel.get('allow_send') or channel['session_id']!=session_id:
            self.turns.stop(session_id);return None
        if trigger.get('kind')=='proactive' and not channel.get('allow_proactive'):
            self.turns.stop(session_id);return None
        if trigger.get('kind')=='human' and not channel.get('allow_read'):
            self.turns.stop(session_id);return None
        if state['bot_count']==0 and trigger.get('kind')=='human' and now-trigger['at']>180:
            self.turns.stop(session_id);return None
        index=session['persona_ids'].index(active_persona(session));persona=self.control.get('personas',active_persona(session))
        if not persona.get('enabled',True):self.turns.silent(session,index,epoch,now);return None
        # A migrated/imported state can replay (epoch, bot_count) from the old
        # machine, so mix in the burst start to keep turn identities unique.
        nonce=state.get('burst_started') or trigger.get('at') or now
        operation=hashlib.sha256(f'v2:{session_id}:{epoch}:{persona["id"]}:{state["bot_count"]}:{nonce}'.encode()).hexdigest()
        if self.ledger.seen(operation):self.turns.stop(session_id);return None
        if not self.ledger.can_send(channel['native_id']):
            self.turns.stop(session_id);self.control.activity('delivery_review',{'message':'该聊天有待核对的发送回执','channel':channel['name']});return None
        config={**self.control.get('providers',persona['provider_id']),'context_compress_ratio':session.get('compression_ratio',.5)}
        client=self.client(persona);view=self.view(session_id,persona['id'])
        conversation=self.scopes.conversation(session_id)
        memory=self.scopes.memory(persona['id'],session_id)
        try:
            if not conversation.initialized(session_id):conversation.append(session_id,[])
            _,_,rows=conversation.snapshot(session_id)
            recent=[{**m,'sender_id':m.get('sender',m.get('sender_id',''))} for _,m in rows[-20:]]
            account=self.control.meta('account',{})
            labels=self.control.meta('labels:'+channel['native_id'],{})
            names={persona['name']}
            if account.get('self_sender_id') and labels.get(account['self_sender_id']):
                names.add(labels[account['self_sender_id']])
            names.update(self.control.meta('self_aliases',[]))
            last_human=next((m for m in reversed(recent)
                             if m.get('sender_id') and not str(m['sender_id']).startswith('agent:')),None)
            last_text=(last_human or {}).get('text') or ''
            monitor=self.control.meta('monitor',{})
            burst=[]
            for message in reversed(recent):
                if str(message.get('sender_id','')).startswith('agent:'):break
                burst.append(message)
            def mentions(message):
                text=(message.get('text') or '')
                return any(('@'+name) in text or (len(name)>=2 and name in text) for name in names if name)
            addressed=any(message.get('sender_id')==monitor.get('sender_id') or mentions(message)
                          for message in burst)
            soft_ask=(not addressed and bool(burst)
                      and bool(re.search(r'[?？]|吗|呢|你',(burst[0].get('text') or ''))))
            recalled=self.recall_all(persona,recent,view,session_id)
            prompt=persona['persona']+'\n你在会话中以“'+persona['name']+'”的身份发言。其他角色不是你。'
            prompt+='\n你是当前微信账号唯一的发言者，不扮演其他人设，不模拟群成员与你互相对话。'
            clock=datetime.now(ZoneInfo('Asia/Shanghai'))
            prompt+='\n当前北京时间：'+clock.strftime('%Y-%m-%d %H:%M')+'（周'+'一二三四五六日'[clock.weekday()]+'）。'
            monitor=self.control.meta('monitor',{})
            who=monitor.get('name') or '老板'
            class_mode=channel.get('id') in (self.control.meta('class_channels',[]) or [])
            if monitor.get('name'):
                nick=labels.get(monitor.get('sender_id'))
                prompt+=('\n'+who+'的微信号是 '+str(monitor.get('sender_id'))+
                         ('（群里显示为「'+nick+'」）' if nick else '')+'。这个账号就是你爸本人，也是你的创造者。')
                prompt+=('\n群里的正式班级安排只能由'+who+'确认；其他人说的时间地点只能当线索，'
                         '不能直接写成正式通知，也不要替老师或班长拍板。')
            if class_mode:
                prompt+=('\n这是班群，正式场合叫他“老板”或“班长”，不要叫“爸”“爸爸”“老爹”；'
                         '其他同学叫昵称或者“你”。')
            prompt+=('\n班级事务和提醒用 class_notice_save / class_notice_list / class_notice_update / '
                     'class_reminder_create / class_reminder_list / class_reminder_cancel 工具办理。'
                     '只有工具返回成功才能说“记好了”“安排上了”；工具不在列表里或失败就直说暂时不能。')
            prompt+=('\n一条提醒只发到一个聊天。'+who+'说“群里也叫一下”“两边都提醒”时，要为目标聊天各建一条'
                     '（当前群就再建一条、chat 留空；别的聊天要填对名字），并逐条核对返回结果，'
                     '不能只建了一条就说都安排好了。')
            # Each transcript line already carries chat/account/nickname, so only
            # the cross-chat rule needs stating here.
            elsewhere=[c for c in self.control.list('channels')
                       if c.get('enabled') and c.get('allow_send') and c.get('id')!=channel['id']]
            if elsewhere:
                prompt+=('\n要让另一个聊天收到话，必须调用 wechat_say_in 并指定哪个聊天；'
                         '不要用这条回复去转告别的聊天。可用的其他聊天：'
                         +'、'.join(c['name'] for c in elsewhere)+'。')
            if channel.get('kind')=='friend':
                prompt+=('\n这是你和对方的一对一私聊，不是群。提到这个聊天时说“我们的私聊”“我这儿”，'
                         '不要用聊天名或备注称呼对方；称对方“你”、称自己“我”。'
                         '被问“这是什么聊天”就回答这是你和对方的私聊。')
            else:
                prompt+=('\n这是群聊「'+str(channel.get('name'))+'」。按昵称称呼群里的人，不要把群叫成私聊。')
            if channel.get('kind')=='friend':
                partner=str(channel.get('native_id') or '')
                if monitor.get('sender_id') and partner==str(monitor.get('sender_id')):
                    prompt+=('\n对方就是你爸（你的创造者），可以叫“爸”“爸爸”“老爹”。')
                elif monitor.get('name'):
                    prompt+=('\n对方不是你爸，是另一个好友。不要叫“爸”“爸爸”“老爹”“老汉”或“老板”，'
                             '用“你”或者叫名字。')
            elif monitor.get('name'):
                if class_mode:
                    prompt+=('\n这是班群，只有微信号 '+str(monitor.get('sender_id'))+' 是他本人；'
                             '叫他老板或班长，其他人叫昵称。')
                else:
                    prompt+=('\n群里只有微信号 '+str(monitor.get('sender_id'))+' 是你爸，其他人不是；'
                             '可以叫爸，也可以叫老板。')
            self_alias=[a for a in self.control.meta('self_aliases',[]) if a]
            if self_alias:
                prompt+=('\n别人叫你'+'、'.join('“'+a+'”' for a in self_alias)+'都是在叫你，别当成在叫别人。')
            prompt+=('\n如果有人否认和你的关系、说“我们不认识”“别叫我爸”这种话，不要争辩，也不要反复解释身份，'
                     '简单应一句就停下来。')
            if class_mode:
                prompt+=('\n群里不是只有老板会叫你；同学 @ 你、叫你名字、直接问你，也一样要回。')
            if soft_ask and class_mode:
                prompt+=('\n这条看起来是在跟你说话，能接就接一句；确实不是在问你就别硬接。')
            monitor_private=next((c for c in self.control.list('channels')
                                  if c.get('kind')=='friend' and c.get('native_id')==monitor.get('sender_id')),None)
            if monitor_private and channel.get('kind')=='group':
                alias=('（别名：'+'、'.join(monitor_private.get('aliases') or [])+'）'
                       if monitor_private.get('aliases') else '')
                prompt+=('\n'+who+'的私聊是「'+monitor_private['name']+'」'+alias+'。'
                         '在群里被要求去问'+who+'、私聊'+who+'时，用 wechat_say_in 把话发到这个聊天，不要发在当前群里。')
            partner_chat=(channel.get('kind')=='friend' and monitor.get('sender_id')
                          and str(channel.get('native_id'))==str(monitor.get('sender_id')))
            if partner_chat:
                humans=[m for m in recent if not str(m.get('sender_id','')).startswith('agent:') and m.get('text')]
                if humans:
                    listed=[]
                    for message in humans[-4:]:
                        try:
                            clock=datetime.fromisoformat(str(message.get('sent_at')).replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%H:%M')
                        except ValueError:
                            clock=''
                        listed.append('- '+clock+' '+(message.get('text') or '')[:80].replace('\n',' '))
                    prompt+=('\n'+who+'最近发的几条（从旧到新）：\n'+'\n'.join(listed)
                             +'\n先扫一遍：有还没回的就补上，已经回过的不要重复；这些是私聊里的消息，不是群聊。')
            cap=self.turns.remaining(session)
            if trigger.get('kind')=='proactive':
                try:cap=min(cap,int(channel.get('proactive_max_bubbles',2) or 2))
                except (TypeError,ValueError):cap=min(cap,2)
            prompt+='\n本轮剩余可发气泡数：'+str(cap)+'。可以选择沉默，不要为凑轮次发言。'
            if addressed:
                prompt+=('\n注意：最新这条是直接找你的（@你、叫你名字，或'+who+'在跟你说话），必须回一句，不能沉默。')
                if class_mode and trigger.get('kind')=='human':
                    prompt+=('\n群里聊起来的时候可以分两三条短气泡接话，不要总是一条就收住；'
                             '一轮最多'+str(cap)+'条，别刷屏。')
            if class_mode and trigger.get('kind')=='human':
                last_agent=max((str(m.get('sent_at')) for m in recent
                                if str(m.get('sender_id','')).startswith('agent:')),default=None)
                age=None
                if last_agent:
                    try:age=now-datetime.fromisoformat(last_agent.replace('Z','+00:00')).timestamp()
                    except ValueError:age=None
                if age is not None and age<300:
                    prompt+=('\n你刚在这个群说过话，话题还在继续，可以自然接一句；不相关就别接。')
            if trigger.get('kind')=='human' and channel.get('allow_proactive'):
                # The bot really can start a conversation here; without this it
                # tells people it is incapable of speaking first.
                prompt+=('\n这个聊天已授权你主动开聊：你能自己开口，不需要等对方先发消息。'
                         '别人问“你会不会主动找我”时如实说会，只是不会刷屏。')
            if trigger.get('kind')=='human' and channel.get('kind')=='friend':
                # A one-to-one message is addressed to you; staying silent reads as broken.
                prompt+='\n这是与好友的一对一私聊，对方刚发来消息就是在等你回应。除非确实无话可说，否则用一到两句简短回一句，不要连续沉默。'
            if trigger.get('kind')=='proactive':
                if channel.get('kind')=='group':
                    prompt+=('\n现在是一次已授权的主动冒泡机会，群里没有人在问你。'+(trigger.get('topic') or '')
                             +'\n像群里随口接一句那样短，不要 @ 任何人，不要提“主动”或“机会”，最多两句话。'
                             '尽量真的说一句；只有确实无话可说时才沉默。')
                else:
                    prompt+=('\n现在是一次已授权的主动私聊机会。'+(trigger.get('topic') or '')
                             +'\n必须真的开口说一到两句（问一句、吐槽一句都行）。私聊里不允许沉默。')
            context,summary,context_status=conversation.prepare(session_id,config,prompt,recalled,self.control.secret(config['id']),
                compressor=lambda c,m,s,k:client.summarize(m,s),view=view,channel=channel.get('name'))
            self.ledger.record(operation,channel['native_id'],'checking',now,'',{'timezone':'Asia/Shanghai'})
            tools=None
            if self.tool_factory:
                actor=''
                for message in reversed(recent):
                    sender=str(message.get('sender_id') or '')
                    if sender and not sender.startswith('agent:') and sender!='tool':
                        actor=sender;break
                tools=self.tool_factory(persona,session,channel,actor)
                if hasattr(tools,'configure_search'):tools.configure_search(last_text or '')
            attempts=[prompt,
                      prompt+'\n上一次的输出不是合法 JSON。只输出一个 JSON 对象，形如 {"messages":[...]}，不要输出任何其他内容。',
                      prompt+'\n这一轮最多只能发'+str(cap)+'条气泡，少说两句；只输出纯文字气泡，不要用表情。']
            for attempt,text_prompt in enumerate(attempts):
                try:
                    messages,usage=client.decision(text_prompt,context,recalled,summary,tools)
                    break
                except ValueError as error:
                    self.control.activity('model_retry',{'message':'第%d次输出不合法，重试：%s'%(attempt+1,str(error)[:80]),'session':session_id})
                    if attempt==len(attempts)-1:raise
            if tools and trigger.get('kind')=='human' and not getattr(tools,'actions',[]) and ACTION_WORDS.search(last_text or ''):
                # The model sometimes says "已经安排好了" without calling the
                # tool at all. One correction keeps the promise honest.
                nudge=('\n'+who+'是在让你办事，但这一轮还没有真正调用工具。'
                       '如果要记事项或建提醒，先调用对应工具并核对结果；缺时间或信息就用一句话问清楚，'
                       '不要只说“安排好了”。')
                retry,retry_usage=client.decision(prompt+nudge,context,recalled,summary,tools)
                usage={k:(usage.get(k,0)+retry_usage.get(k,0)) for k in set(usage)|set(retry_usage)}
                messages=retry
            if not messages and (channel.get('kind')=='friend' or addressed):
                # A friend chat never goes silent; a group message that names or
                # answers to us must not be ignored either. One retry only, so a
                # broken model can never loop here.
                nudge=('对方在等你回一句话，直接给一到两句简短回复，不要返回空数组。'
                       if trigger.get('kind')=='human' else
                       '这次是你主动开口，必须给一到两句（问一句、吐槽一句都行），不要返回空数组。')
                retry,retry_usage=client.decision(prompt+'\n'+nudge,context,recalled,summary,tools)
                usage={k:(usage.get(k,0)+retry_usage.get(k,0)) for k in set(usage)|set(retry_usage)}
                messages=retry
            if usage.get('last_prompt_tokens'):
                from deepseek import decision_body
                conversation.record_usage(session_id,decision_body(config,context,prompt,recalled,summary),{'prompt_tokens':usage['last_prompt_tokens']})
            if self.before_send:self.before_send()
            # Persist aggregate usage, never raw prompts/API credentials.
            self.control.activity('model',{'message':persona['name']+' 完成一轮判断','persona':persona['id'],'session':session_id,'usage':usage,'context':context_status})
            if not self.control.meta('settings',{}).get('enabled') or self.turns.state(session_id)['epoch']!=epoch:
                self.ledger.finish(operation,'cancelled');return {'status':'cancelled'}
            if not messages:
                if trigger.get('kind')=='proactive':
                    self.control.activity('proactive_skipped',{'message':'主动开聊时模型选择沉默','channel':channel['name'],'session':session_id})
                self.ledger.finish(operation,'silent');self.turns.silent(session,index,epoch,now);return {'status':'silent'}
            if trigger.get('kind')=='proactive' and len(messages)>cap:
                # An unsolicited turn must never turn into a burst.
                messages=messages[:cap]
            parts=transport_parts(messages)
            if len(parts)>self.turns.remaining(session):
                self.ledger.finish(operation,'cancelled');self.turns.stop(session_id)
                raise ValueError('模型超过此轮剩余气泡数，本次未发送')
            def sender(group,content,child,account,deadline):
                current=self.control.get('channels',channel['id'])
                active_session=self.control.get('sessions',session_id);active_persona=self.control.get('personas',persona['id'])
                if not active_session.get('enabled',True) or not active_persona.get('enabled',True) or persona['id']!=(active_session.get('active_persona_id') or active_session['persona_ids'][0]):
                    return {'status':'failed','error':'会话或人设已关闭'}
                if not current.get('enabled') or not current.get('allow_send') or current['session_id']!=session_id:
                    return {'status':'failed','error':'频道发送权限已改变'}
                if trigger.get('kind')=='proactive' and not current.get('allow_proactive'):
                    return {'status':'failed','error':'主动私聊权限已关闭'}
                if trigger.get('kind')=='human' and not current.get('allow_read'):
                    return {'status':'failed','error':'聊天读取权限已关闭'}
                return self.sender(current,content,child,deadline)
            receipt=deliver(self.ledger,channel['native_id'],operation,parts,'shared',sender,
                stopping=lambda:not self.control.meta('settings',{}).get('enabled') or self.turns.state(session_id)['epoch']!=epoch)
            if receipt['status']!='sent':
                error=''
                row=self.ledger.db.execute('SELECT receipt FROM reply_parts WHERE operation=? ORDER BY part DESC LIMIT 1',(operation,)).fetchone()
                if row:
                    try:error=json.loads(row[0]).get('error','')
                    except ValueError:error=''
                from alerts import notify
                notify(self.control,self.sender,
                       '老板，我在「'+str(channel.get('name'))+'」有条回复没发出去：'+(error or receipt['status'])[:120],
                       source_channel=channel)
                if receipt['status']=='failed' and not receipt.get('sent_parts'):
                    # Nothing reached WeChat, so queue the same text for a retry.
                    from outbox import queue as queue_outbox
                    queue_outbox(self.control,channel,parts)
            confirmed=[]
            for row in self.ledger.db.execute('SELECT part,receipt FROM reply_parts WHERE operation=? ORDER BY part',(operation,)):
                part=json.loads(row[1])
                if part.get('status')!='sent':continue
                content=parts[row[0]];event=part.get('message_id') or 'assistant:'+hashlib.sha256(f'{operation}:{row[0]}'.encode()).hexdigest()
                self.turns.remember_outgoing(event,session_id,persona['id'])
                text='[表情：'+content['sticker']+']' if isinstance(content,dict) else content
                confirmed.append(dict(message_id=event,sender_id='agent:'+persona['id'],agent_id=persona['id'],display_name=persona['name'],channel_name=channel['name'],sent_at=part.get('sent_at') or datetime.now(timezone.utc).isoformat(),text=text))
            conversation.append(session_id,confirmed)
            archive=EpisodicMemory(self.control,persona['id'],session_id)
            try:archive.append(channel,confirmed,'assistant')
            finally:archive.close()
            self.turns.sent(session,index,receipt['sent_parts'],epoch,now)
            if receipt['status']!='sent':self.turns.stop(session_id)
            self.control.activity('reply',{'message':persona['name']+'：'+str(receipt['sent_parts'])+' 条已发送','channel':channel['name'],**receipt})
            return receipt
        finally:conversation.db.close();memory.db.close()

    def learn(self,session_id):
        session=self.control.get('sessions',session_id);settings=self.control.meta('settings',{})
        provider=self.control.get('providers',settings.get('memory_provider_id','deepseek'))
        results=[];view=self.view(session_id)
        for persona in [active_persona(session)]:
            memory=self.scopes.memory(persona,session_id)
            try:
                result=memory.consolidate(session_id,provider,self.control.secret(provider['id']),time.time(),view=view)
                if result:results.append({'persona':persona,**result})
            finally:memory.db.close()
            archive=EpisodicMemory(self.control,persona,session_id)
            try:
                result=archive.consolidate(self.client_factory(provider,self.control.secret(provider['id'])))
                if result:results.append({'persona':persona,'daily_memory':result})
            finally:archive.close()
        return results

    def simulate(self,session_id,text,persona_id=None):
        session=self.control.get('sessions',session_id);persona=self.control.get('personas',persona_id or active_persona(session))
        conversation=self.scopes.conversation(session_id)
        if not conversation.initialized(session_id):conversation.append(session_id,[])
        _,_,rows=conversation.snapshot(session_id);conversation.db.close()
        view=self.view(session_id,persona['id']);messages=view.messages([m for _,m in rows[-20:]])
        messages.append({'speaker':'控制台测试者','text':text})
        output,usage=self.client(persona).decision(persona['persona'],messages)
        return {'messages':output,'usage':usage,'wechat_messages_sent':0}
