import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from model_view import GroupView, group_nicknames
from deepseek import decision_body


def test_native_group_nickname_field_and_nickname_fallback():
    member = b'\x0a\x06wxid_a\x12\x03Bob'
    assert group_nicknames(b'\x0a'+bytes([len(member)])+member) == {'wxid_a':'Bob'}
    class Connection:
        def execute(self,sql,args):
            if 'ext_buffer' in sql:
                class Result:
                    def fetchone(self):
                        other=b'\x0a\x06wxid_b'
                        return (b'\x0a'+bytes([len(member)])+member+b'\x0a'+bytes([len(other)])+other,)
                return Result()
            return [('wxid_a','Profile name'),('wxid_b','Nickname B')]
        def close(self):
            pass
    class Reader:
        def _database(self,name): return name
        def _open(self,item): return Connection(),None
    view=GroupView.from_reader(Reader(),'group@chatroom','bot')
    assert view.name('wxid_a')=='Bob'
    assert view.name('wxid_b')=='Nickname B'


def test_model_gets_selected_identity_fields_without_native_receipts_or_transport_prefix():
    view=GroupView({'wxid_a':'群昵称'},'bot')
    messages=view.messages([dict(message_id='server:opaque',sender_id='wxid_a',
        sent_at='2026-10-25T06:00:00Z',text='wxid_a:\n你好',source={'file_ref':'secret'})])
    body=decision_body({'model':'deepseek-flash'},messages,'角色',
                       view.memories([{'subject_id':'wxid_a','fact':'wxid_a 喜欢米饭','source_ids':['server:opaque']}]))
    value=body['messages'][1]['content']
    assert '[微信号:wxid_a｜昵称:群昵称] 你好' in value and '群昵称 喜欢米饭' in value
    assert 'wxid_a:\n你好' not in value
    for garbage in ['server:opaque','message_id','source_ids','file_ref','max_reply_chars']:
        assert garbage not in value


def test_shared_chat_labels_do_not_change_the_memory_speaker_identity():
    view=GroupView({'person':'测试成员'})
    shown=view.messages([dict(sender_id='person',display_name='测试成员',channel_name='私聊',text='我喜欢米饭')])
    assert shown[0]['speaker']=='测试成员' and shown[0]['chat']=='私聊'
    assert '[聊天:私聊｜微信号:person｜昵称:测试成员] 我喜欢米饭' in decision_body({'model':'deepseek-flash'},shown,'角色')['messages'][1]['content']
