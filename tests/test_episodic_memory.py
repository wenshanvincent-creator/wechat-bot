import json
import sys
import sqlite3
from datetime import datetime,timezone
from pathlib import Path
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from control_store import ControlStore
from episodic_memory import EpisodicMemory,backfill
from conversation_context import ConversationContext
from agent_tools import AgentTools


class Counter:
    def count(self,body):return len(str(body))


def setup(tmp_path):
    control=ControlStore(tmp_path);control.seed({'groups':['group@chatroom']},'固定的人设，不应被改写','unit-key')
    return control,control.get('channels','local-0')


def message(i,text='小明说下周一起打羽毛球。',role='human'):
    return dict(message_id='event-'+str(i),sender_id='member' if role=='human' else 'agent:whale',display_name='小明' if role=='human' else '鲸鱼娘',sent_at='2026-10-23T12:00:00+08:00',text=text)


def test_originals_survive_context_compaction_and_remain_literal_not_vector_search(tmp_path):
    c,ch=setup(tmp_path);archive=EpisodicMemory(c,'whale','main');conversation=ConversationContext(c.conversation_path('main'),'shared',Counter())
    rows=[message(i,'原话テスト喜欢羽毛球%_ '+'聊天'*50) for i in range(25)]
    archive.append(ch,rows,'human');archive.append(ch,rows,'human');conversation.append('main',rows)
    cfg=dict(model='test',context_window_tokens=18000,context_compress_ratio=.5)
    _,_,status=conversation.prepare('main',cfg,'人设',[], '',compressor=lambda *a:'旧消息已总结')
    assert status['compressed']
    assert len(conversation.snapshot('main')[2])<25
    assert archive.overview()['originals']==25
    found=archive.search('テスト喜欢羽毛球%_')['messages'][0]
    assert archive.read(found['record'])['text']==rows[-1]['text']
    archive.close();conversation.db.close()


def test_search_and_daily_notes_obey_current_source_permissions_and_namespace(tmp_path):
    c,ch=setup(tmp_path);memory=EpisodicMemory(c,'whale','main');memory.append(ch,[message(1)],'human')
    c.put('personas',dict(id='other',name='另一人设',provider_id='deepseek',persona='另一人设',enabled=True))
    c.put('sessions',dict(id='another',name='另一会话',persona_ids=['other'],enabled=True))
    isolated=EpisodicMemory(c,'other','another');assert isolated.search('羽毛球')['messages']==[];isolated.close()
    record=memory.search('羽毛球')['messages'][0]['record']
    with memory.db:memory.db.execute('INSERT INTO relationship_notes VALUES (?,?,?,?)',('local-0','相处中知道小明喜欢羽毛球','[]',0))
    c.put('channels',{**ch,'allow_read':False})
    assert memory.search('羽毛球')['messages']==[] and memory.recall()==[]
    with pytest.raises(PermissionError):memory.read(record)
    assert memory.append(ch,[message(2)],'human')==0
    memory.close()


def test_native_sender_prefix_stays_in_original_but_not_model_facing_memory(tmp_path):
    c,ch=setup(tmp_path);memory=EpisodicMemory(c,'whale','main')
    text='wxid_test_member:\n大肥鱼看到了回一下'
    memory.append(ch,[{**message(1,text),'sender_id':'wxid_test_member'}],'human')
    result=memory.search('大肥鱼')['messages'][0]
    assert result['speaker']=='小明' and result['text']=='大肥鱼看到了回一下'
    assert memory.read(result['record'])['text']==result['text']
    assert memory.db.execute('SELECT text FROM archive_messages').fetchone()[0]==text
    class Client:
        def complete(self,messages,**kwargs):
            assert 'wxid_test_member' not in messages[1]['content']
            return {'content':json.dumps(dict(diary='',relationship='',lines=[]))},{}
    assert memory.consolidate(Client(),datetime(2026,10,25,tzinfo=timezone.utc).timestamp())['processed']==1
    memory.close()


def test_daily_recollection_has_source_evidence_does_not_rewrite_persona_and_deduplicates(tmp_path):
    c,ch=setup(tmp_path);memory=EpisodicMemory(c,'whale','main');memory.append(ch,[message(1)],'human')
    original=c.get('personas','whale')['persona'];calls=[]
    class Client:
        def complete(self,messages,**kwargs):
            calls.append(messages);assert 'sender_id' not in messages[1]['content'] and 'member' not in messages[1]['content']
            return {'content':json.dumps(dict(diary='小明说下周一起打羽毛球。',relationship='小明提过一起打羽毛球的计划。',lines=[1]))},{}
    now=datetime(2026,10,25,tzinfo=timezone.utc).timestamp()
    assert memory.consolidate(Client(),now)['processed']==1
    assert memory.consolidate(Client(),now+301) is None
    assert len(calls)==1 and '羽毛球' in memory.recall()[0]
    assert c.get('personas','whale')['persona']==original
    assert memory.overview()['diaries'][0]['day']=='2026-10-23'
    memory.close()


def test_invalid_diary_evidence_keeps_originals_and_does_not_advance_progress(tmp_path):
    c,ch=setup(tmp_path);memory=EpisodicMemory(c,'whale','main');memory.append(ch,[message(1)],'human')
    class Client:
        def complete(self,*a,**kw):return {'content':json.dumps(dict(diary='编造的回忆',relationship='',lines=[999]))},{}
    now=datetime(2026,10,25,tzinfo=timezone.utc).timestamp()
    with pytest.raises(ValueError):memory.consolidate(Client(),now)
    assert memory.overview()['originals']==1 and memory.overview()['diaries']==[]
    assert memory.consolidate(Client(),now+1) is None
    memory.close()


def test_backfill_only_surviving_originals_and_excludes_tool_logs(tmp_path):
    c,ch=setup(tmp_path);conversation=ConversationContext(c.conversation_path('main'),'shared',Counter())
    conversation.append('main',[{**message(1),'channel_name':ch['name']},{**message(2,'shell-secret-log'),'sender_id':'tool','channel_name':ch['name']}])
    assert backfill(c)=={'copied':1,'skipped':1}
    assert backfill(c)=={'copied':0,'skipped':1}
    memory=EpisodicMemory(c,'whale','main');assert memory.search('shell-secret-log')['messages']==[];memory.close();conversation.db.close()


def test_memory_tools_stay_available_without_workspace_or_friend_tools(tmp_path):
    c,ch=setup(tmp_path);persona=c.get('personas','whale');persona['tool_access']=False
    tools=AgentTools(c,persona,c.get('sessions','main'),ch)
    assert {t['function']['name'] for t in tools.schemas}=={'memory_search','memory_read','memory_diary'}
    assert tools.execute('memory_search',{'query':'不存在'})['messages']==[]
    with pytest.raises(ValueError):tools.execute('web_fetch',{'url':'https://example.com'})


def test_legacy_backfill_requires_original_source_receipts_instead_of_guessing(tmp_path):
    c,ch=setup(tmp_path);c.set_meta('v1_migrated',True)
    legacy=sqlite3.connect(tmp_path/'context.sqlite');legacy.execute('CREATE TABLE turns(message_id TEXT,group_id TEXT)')
    legacy.executemany('INSERT INTO turns VALUES (?,?)',[('event-1',ch['native_id']),('event-2','not-approved@chatroom')]);legacy.commit();legacy.close()
    conversation=ConversationContext(c.conversation_path('main'),'shared',Counter());conversation.append('main',[message(1),message(2)])
    assert backfill(c)=={'copied':1,'skipped':1}
    memory=EpisodicMemory(c,'whale','main');assert memory.overview()['originals']==1;memory.close();conversation.db.close()


def test_imported_account_speech_is_not_falsely_attributed_to_ai(tmp_path):
    c,ch=setup(tmp_path);conversation=ConversationContext(c.conversation_path('main'),'shared',Counter())
    conversation.append('main',[{**message(1,'我在人工配置登录。',role='assistant'),'agent_id':'whale','channel_name':ch['name']}])
    backfill(c);memory=EpisodicMemory(c,'whale','main');row=memory.search('人工配置')['messages'][0]
    assert row['role']=='account' and '操作者未确认' in row['speaker']
    memory.close();conversation.db.close()
