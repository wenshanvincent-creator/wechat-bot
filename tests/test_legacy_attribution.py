import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from control_store import ControlStore
from conversation_context import ConversationContext
from migrate_v1 import attribute_legacy_agent
from agent_kernel import SessionView


def test_old_physical_account_messages_belong_only_to_original_persona(tmp_path):
    store=ControlStore(tmp_path);store.seed({'groups':[]},'角色','fake-key');store.set_meta('account',{'self_sender_id':'wxid_bot'});store.set_meta('v1_migrated',True)
    conversation=ConversationContext(store.conversation_path('main'),'shared',None)
    conversation.append('main',[dict(message_id='old',sender_id='wxid_bot',display_name='旧昵称（你）',text='旧机器人消息',sent_at='2026-10-25T00:00:00Z')]);conversation.db.close()
    attribute_legacy_agent(store)
    connection=sqlite3.connect(store.conversation_path('main'));message=json.loads(connection.execute('SELECT payload FROM turns').fetchone()[0])
    assert message['agent_id']=='whale'
    assert '（你）' in SessionView(agent_id='whale').messages([message])[0]['speaker']
    assert '（你）' not in SessionView(agent_id='second').messages([message])[0]['speaker']
