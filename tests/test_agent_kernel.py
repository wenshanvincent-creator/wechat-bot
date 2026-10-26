import sys
from datetime import datetime,timezone
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from control_store import ControlStore
from agent_state import AgentState
from agent_kernel import AgentKernel
from model_view import GroupView


class Counter:
    def count(self,body):return len(str(body))


def setup(tmp_path):
    control=ControlStore(tmp_path);control.seed({'groups':['g@chatroom']},'角色A','fake-key')
    control.set_meta('settings',{**control.meta('settings'),'enabled':True,'debounce_ms':0})
    return control


def test_multipart_and_tool_continuation_share_budget_until_new_human_event(tmp_path):
    control=setup(tmp_path);turns=AgentState(control)
    session=control.get('sessions','main');session['max_bot_messages_without_human']=3
    assert turns.human('main','human-1','local-0',100,0)
    assert turns.due(session,100)
    epoch=turns.state('main')['epoch']
    turns.sent(session,0,2,epoch,101)
    assert turns.remaining(session)==1
    restarted=AgentState(ControlStore(tmp_path))
    assert restarted.remaining(session)==1
    assert not restarted.due(session,102)
    restarted.resume(session,'local-0','whale')
    assert not restarted.human('main','human-1','local-0',102,0)
    restarted.sent(session,0,1,epoch,103)
    assert not restarted.due(session,104)
    assert restarted.human('main','human-2','local-0',105,0)
    assert restarted.remaining(session)==3


def test_only_active_persona_replies_and_switching_keeps_separate_memory_files(tmp_path):
    control=setup(tmp_path)
    control.put('personas',dict(id='second',name='角色B',provider_id='deepseek',persona='角色B性格',enabled=True))
    session=control.get('sessions','main');session.update(persona_ids=['whale','second'],max_bot_messages_without_human=3)
    control.put('sessions',session)
    captured=[];sent=[]
    class Client:
        def __init__(self,*args):pass
        def decision(self,persona,messages,*args):
            captured.append((persona,messages));return ['第一条','第二条'],{}
    def sender(channel,content,operation,deadline):
        sent.append(content);return {'status':'sent','message_id':'native:'+operation}
    kernel=AgentKernel(control,Counter(),sender,Client)
    channel=control.get('channels','local-0')
    message=dict(message_id='human',sender_id='person',sent_at=datetime.fromtimestamp(100,timezone.utc).isoformat(),text='大家好')
    assert kernel.ingest(channel,[message],GroupView({'person':'小明'}),100)==1
    assert not control.memory_path('second','main').exists()
    assert kernel.step('main',100)['sent_parts']==2
    assert kernel.step('main',101) is None
    assert len(captured)==1
    assert sent==['第一条','第二条']
    assert control.db.execute("SELECT COUNT(*) FROM outgoing_events WHERE agent_id='second'").fetchone()[0]==0
    control.put('sessions',{**control.get('sessions','main'),'active_persona_id':'second'})
    message={**message,'message_id':'human-2','sent_at':datetime.fromtimestamp(102,timezone.utc).isoformat()}
    assert kernel.ingest(channel,[message],GroupView({'person':'小明'}),102)==1
    assert kernel.step('main',102)['sent_parts']==2
    assert kernel.step('main',103) is None
    assert '角色B性格' in captured[1][0]
    assert any('鲸鱼娘' in m['speaker'] for m in captured[1][1])
    assert 'source_ids' not in str(captured[0][1])
    assert control.memory_path('whale','main')!=control.memory_path('second','main')
    assert kernel.turns.is_outgoing(next(iter(kernel.control.db.execute('SELECT id FROM outgoing_events')))[0])


def test_silence_does_not_offer_the_same_message_to_another_persona(tmp_path):
    control=setup(tmp_path)
    control.put('personas',dict(id='second',name='角色B',provider_id='deepseek',persona='性格B',enabled=True))
    control.put('sessions',{**control.get('sessions','main'),'persona_ids':['whale','second']})
    calls=[]
    class Client:
        def __init__(self,*args):pass
        def decision(self,*args):calls.append(1);return [],{}
    kernel=AgentKernel(control,Counter(),lambda *a:None,Client)
    kernel.turns.human('main','human','local-0',100,0)
    control.set_meta('trigger:main',{'kind':'human','at':100})
    assert kernel.step('main',100)['status']=='silent'
    assert kernel.step('main',101) is None
    assert calls==[1]


def test_switching_persona_cancels_inflight_old_reply_and_tool_resume(tmp_path):
    control=setup(tmp_path)
    control.put('personas',dict(id='second',name='角色B',provider_id='deepseek',persona='性格B',enabled=True))
    session={**control.get('sessions','main'),'persona_ids':['whale','second']}
    control.put('sessions',session)
    class Client:
        def __init__(self,*args):pass
        def decision(self,*args):
            control.put('sessions',{**session,'active_persona_id':'second'})
            return ['旧人设消息'],{}
    kernel=AgentKernel(control,Counter(),lambda *a:(_ for _ in ()).throw(AssertionError('Old persona sent')),Client)
    kernel.turns.human('main','human','local-0',100,0)
    control.set_meta('trigger:main',{'kind':'human','at':100})
    assert kernel.step('main',100)['status']=='cancelled'
    kernel.turns.resume(control.get('sessions','main'),'local-0','whale')
    assert not kernel.turns.due(control.get('sessions','main'),101)


def test_disabled_read_and_send_are_enforced_at_the_engine_boundary(tmp_path):
    control=setup(tmp_path);channel=control.get('channels','local-0')
    kernel=AgentKernel(control,Counter(),lambda *args: (_ for _ in ()).throw(AssertionError('Unauthorized send')))
    message=dict(message_id='human',sender_id='person',sent_at=datetime.now(timezone.utc).isoformat(),text='hello')
    control.put('channels',{**channel,'allow_read':False})
    assert kernel.ingest(channel,[message],GroupView())==0
    control.put('channels',{**channel,'allow_send':False})
    assert kernel.ingest(channel,[message],GroupView())==1
    assert kernel.step('main') is None


def test_stale_generation_is_cancelled_when_a_new_human_event_arrives(tmp_path):
    control=setup(tmp_path);channel=control.get('channels','local-0');holder={}
    class Client:
        def __init__(self,*args):pass
        def decision(self,*args):
            holder['kernel'].turns.human('main','new-event','local-0',101,0)
            return ['过时的回复'],{}
    kernel=holder['kernel']=AgentKernel(control,Counter(),lambda *args: (_ for _ in ()).throw(AssertionError('Stale send')),Client)
    kernel.ingest(channel,[dict(message_id='old-event',sender_id='person',sent_at=datetime.fromtimestamp(100,timezone.utc).isoformat(),text='旧问题')],GroupView(),100)
    assert kernel.step('main',100)['status']=='cancelled'
