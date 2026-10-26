import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from control_store import ControlStore


def setup_store(tmp_path):
    store=ControlStore(tmp_path)
    store.seed({'groups':['g@chatroom']},'角色设定','secret-for-unit-test')
    return store


def test_model_key_is_write_only_and_persona_session_memory_files_are_separate(tmp_path):
    store=setup_store(tmp_path)
    assert store.secret('deepseek')=='secret-for-unit-test'
    assert 'secret-for-unit-test' not in json.dumps(store.snapshot())
    provider=store.get('providers','deepseek');provider.update(name='新名字')
    store.put('providers',provider)
    assert store.secret('deepseek')=='secret-for-unit-test'
    store.put('personas',dict(id='second',name='另一个角色',provider_id='deepseek',persona='另一个性格',enabled=True))
    store.put('sessions',dict(id='other',name='第二会话',persona_ids=['whale','second'],enabled=True))
    a=store.memory_path('whale','main');b=store.memory_path('second','main');c=store.memory_path('whale','other')
    assert len({a,b,c})==3
    a.write_text('a');assert not b.exists() and not c.exists()
    assert store.conversation_path('main')!=store.conversation_path('other')
    with pytest.raises(ValueError):store.memory_path('../escape','main')


def test_binding_permissions_are_independent_and_referenced_config_cannot_be_deleted(tmp_path):
    store=setup_store(tmp_path)
    channel=dict(id='friend',name='好友',native_id='friend-id',kind='friend',session_id='main',enabled=True,allow_read=False,allow_send=True,allow_proactive=True)
    store.put('channels',channel)
    assert not store.get('channels','friend')['allow_read']
    with pytest.raises(ValueError):store.put('channels',{**channel,'allow_send':False})
    with pytest.raises(ValueError):store.delete('sessions','main')
    with pytest.raises(ValueError):store.delete('providers','deepseek')


def test_password_sessions_require_valid_token_and_csrf(tmp_path):
    store=setup_store(tmp_path);store.set_password('test-admin-password')
    with pytest.raises(PermissionError):store.login('bad-password')
    token,csrf=store.login('test-admin-password')
    assert store.authenticate(token)==csrf
    assert store.authenticate(token,csrf)==csrf
    with pytest.raises(PermissionError):store.authenticate(token,'bad-csrf')
    with pytest.raises(PermissionError):store.authenticate('bad-token')
    assert 'test-admin-password' not in json.dumps(store.meta('admin_password'))


def test_jobs_are_claimed_once(tmp_path):
    store=setup_store(tmp_path);key=store.enqueue('directory',{})
    assert store.take_job()['id']==key
    assert store.take_job() is None
    store.finish_job(key,'done',{'count':2})
    assert store.jobs()[0]['state']=='done'


def test_single_account_defaults_and_active_persona_validation(tmp_path):
    store=setup_store(tmp_path)
    assert store.meta('settings')['agent_dialogue_enabled'] is False
    assert store.snapshot()['wechat']['multi_account_supported'] is False
    session=store.get('sessions','main')
    assert session['active_persona_id']=='whale'
    with pytest.raises(ValueError):store.put('sessions',{**session,'active_persona_id':'missing'})
