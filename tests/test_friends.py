import os
import base64
import io
from PIL import Image
import sqlite3
import sys
import time
from pathlib import Path
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from control_store import ControlStore
from agent_tools import AgentTools
from friends import Friends,identifier


class Reader:
    account_ref='test-account'
    def __init__(self,path):
        self.path=path
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE contact(username TEXT,alias TEXT,nick_name TEXT,remark TEXT,flag INTEGER,delete_flag INTEGER,verify_flag INTEGER)')
            c.execute("INSERT INTO contact VALUES ('new-native','test_handle','测试新好友','',0,0,0)")
    def _database(self,name):return name
    def _open(self,item):
        c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row;return c,{}
    def list_friends(self):
        with sqlite3.connect(self.path) as c:
            return [dict(conversation_id=r[0],name=r[1],remark='') for r in c.execute('SELECT username,nick_name FROM contact WHERE (flag & 1)=1 AND delete_flag=0')]
    def add(self):
        with sqlite3.connect(self.path) as c:c.execute("UPDATE contact SET flag=3 WHERE username='new-native'")


def setup(tmp_path,ui):
    c=ControlStore(tmp_path/'state');c.seed({'groups':['g@chatroom']},'测试人设','unit-only-key')
    c.set_meta('account',{'account_ref':'test-account'})
    reader=Reader(tmp_path/'native.sqlite')
    class Vision:
        def vision(self,*args,**kwargs):
            if kwargs.get('task','').startswith('只确认'):return {'name':'测试新好友','can_verify':True},{}
            return {'rows':[{'index':0,'name':'测试新好友','state':'pending'}]},{}
    return c,reader,Friends(c,ui,client=Vision(),reader=reader)


def test_friend_tools_only_queue_relationship_jobs_without_changing_chat_acl(tmp_path):
    c=ControlStore(tmp_path);c.seed({'groups':['g@chatroom']},'人设','unit-key')
    before=c.list('channels')
    tools=AgentTools(c,c.get('personas','whale'),c.get('sessions','main'),c.get('channels','local-0'))
    result=tools.execute('wechat_add_friend',{'identifier':'test_handle','verification':'你好，这是好友验证。'})
    assert result['status']=='queued'
    job=c.jobs()[0];assert job['kind']=='friend_add' and job['body']['identifier']=='test_handle'
    assert c.list('channels')==before
    assert not any(j['kind'] in ('send','message') for j in c.jobs())
    with pytest.raises(ValueError):identifier('wxid_internal_123')
    with pytest.raises(ValueError):identifier('只是一个昵称')


def test_accepted_friend_is_not_granted_read_reply_or_proactive_permissions(tmp_path):
    calls=[];holder={}
    def ui(script,body):
        calls.append(body['action'])
        if body['action']=='click':
            assert body['rect']==[56,584,112,32]
            holder['reader'].add()
        if body['action']=='snapshot':
            data=io.BytesIO();Image.new('RGB',(360,640),'white').save(data,format='PNG')
            return {'known_form':'accept_confirmation','image':base64.b64encode(data.getvalue()).decode(),'window_id':1,'header_hash':'header','width':360,'height':640}
        if body['action']=='accept':return {'image':'aW1hZ2U=','profile_hash':'profile','verify_hash':'verify'}
        return {'rows':[{'index':0,'row_hash':'unique-pending-row','top':179}],'image':'aW1hZ2U='}
    c,reader,friends=setup(tmp_path,ui);holder['reader']=reader;before=c.list('channels')
    request=friends.listing()['requests'][0]
    public=c.snapshot()['friend_requests'][0]
    assert set(public)=={'request_id','name','expires_at'}
    result=friends.accept(request['request_id'])
    assert result['status']=='accepted' and result['chat_permissions_changed'] is False
    assert c.list('channels')==before and c.snapshot()['friend_requests']==[]
    assert calls==['requests','cleanup','accept','verify','snapshot','click','cleanup']


@pytest.mark.skipif(os.name!='posix',reason='The native WeChat reader runs in the Linux container')
def test_native_reader_denies_messages_from_an_added_but_unapproved_friend():
    from native_reader import NativeReader
    native=NativeReader.__new__(NativeReader)
    native.private_ids=frozenset();native.list_friends=lambda:[{'conversation_id':'new-native'}];native.list_groups=lambda:{'groups':[]}
    with pytest.raises(ValueError):native.read_messages('new-native')


def test_uncertain_friend_accept_is_not_retried(tmp_path):
    calls=[]
    def ui(script,body):
        calls.append(body['action'])
        if body['action']=='accept':return {'image':'aW1hZ2U=','profile_hash':'profile','verify_hash':'verify'}
        if body['action']=='verify':raise RuntimeError('connection lost after possible click')
        return {}
    c,reader,friends=setup(tmp_path,ui)
    with c.db:c.db.execute('INSERT INTO friend_requests VALUES (?,?,?,?,?,?)',('a'*32,'测试新好友','row','test-account',time.time()+300,'pending'))
    assert friends.accept('a'*32)['status']=='uncertain'
    again=Friends(c,ui,client=friends.client,reader=reader)
    assert again.accept('a'*32)['status']=='uncertain'
    assert calls.count('verify')==1


def test_existing_friend_does_not_receive_duplicate_request_or_message(tmp_path):
    def ui(*args):raise AssertionError('Existing friend should require no UI action')
    c,reader,friends=setup(tmp_path,ui);reader.add();before=c.list('channels')
    assert friends.add('test_handle','你好')['status']=='already_friend'
    assert c.list('channels')==before


def test_expired_or_other_account_requests_cannot_be_accepted(tmp_path):
    def ui(*args):raise AssertionError('Stale request must not reach UI')
    c,reader,friends=setup(tmp_path,ui)
    with c.db:c.db.execute('INSERT INTO friend_requests VALUES (?,?,?,?,?,?)',('a'*32,'测试新好友','row','test-account',time.time()-1,'pending'))
    with pytest.raises(ValueError):friends.accept('a'*32)
    c.set_meta('account',{'account_ref':'other-account'})
    with pytest.raises(PermissionError):Friends(c,ui,client=friends.client,reader=reader)
