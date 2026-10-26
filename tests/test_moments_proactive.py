import sys
from pathlib import Path
from datetime import datetime,timezone
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from control_store import ControlStore
from moments import Moments
from agent_state import AgentState
from proactive import schedule,quiet


def test_publication_has_separate_permission_and_no_retry_after_unknown_ui_result(tmp_path):
    store=ControlStore(tmp_path);store.seed({'groups':[]},'性格','fake-key');store.set_meta('account',{'account_ref':'account'})
    moments=Moments(store);draft=moments.save('测试草稿','private')
    with pytest.raises(PermissionError):moments.publish(draft['id'],lambda *a:None)
    assert moments.list()[0]['state']=='draft'
    assert moments.publish(draft['id'],lambda s,r:{'status':'prepared_not_sent'},True)['status']=='prepared_not_sent'
    store.set_meta('settings',{'moments_enabled':True})
    def crashed(*args):raise TimeoutError()
    with pytest.raises(TimeoutError):moments.publish(draft['id'],crashed)
    with pytest.raises(ValueError):moments.publish(draft['id'],crashed)
    assert moments.list()[0]['state']=='uncertain'


def test_private_acl_and_quiet_hours_block_autonomous_initiation(tmp_path):
    store=ControlStore(tmp_path);store.seed({'groups':[]},'性格','fake-key')
    store.set_meta('settings',{**store.meta('settings'),'enabled':True})
    friend=dict(id='friend',name='好友',native_id='native-friend',kind='friend',session_id='main',enabled=True,allow_read=False,allow_send=True,allow_proactive=True,proactive_interval_minutes=1,quiet_start='23:00',quiet_end='08:00')
    store.put('channels',friend);turns=AgentState(store)
    now=datetime(2026,10,25,4,tzinfo=timezone.utc).timestamp() # noon Shanghai
    schedule(store,turns,now);assert turns.state('main') is None
    schedule(store,turns,now+61);assert turns.state('main')['pending_at']==now+61
    turns.stop('main');store.put('channels',{**friend,'allow_proactive':False})
    schedule(store,turns,now+180);assert turns.state('main')['pending_at'] is None
    assert quiet(friend,datetime(2026,10,25,16,tzinfo=timezone.utc).timestamp(),'Asia/Shanghai')
