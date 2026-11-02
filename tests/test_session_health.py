import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RUNTIME=Path(__file__).resolve().parents[1]/'runtime'
sys.path.insert(0,str(RUNTIME))
from control_store import ControlStore
from native_keys import NativeAccountUnavailable
from native_recovery import live_ready
import session_health
from session_health import SessionHealth,FailureGate,error_code
from wal_snapshot import SnapshotChanged


def activities(control,kind):
    return control.db.execute('SELECT count(*) FROM activity WHERE kind=?',(kind,)).fetchone()[0]


def test_repeated_outage_is_one_alarm_with_bounded_backoff_and_recovery(tmp_path,monkeypatch):
    c=ControlStore(tmp_path);c.set_meta('account',{'account_ref':'bound'})
    monkeypatch.setattr(session_health,'diagnostics',lambda:{'process_identity':[1,1,'old'],'active_root_count':0})
    health=SessionHealth(c);health.start()
    delays=[health.failed(NativeAccountUnavailable('no account')) for _ in range(50)]
    assert delays[:6]==[2,5,10,20,30,60] and max(delays)==60
    assert activities(c,'component_fault')==1
    assert activities(c,'native_session')==2
    assert c.meta('native_session')['state']=='unavailable' and not live_ready(c)
    # A newly constructed monitor retains the persisted fault instead of spamming.
    health=SessionHealth(c);health.failed(NativeAccountUnavailable('same fault'))
    assert activities(c,'component_fault')==1
    health.ready(SimpleNamespace(identity=(2,2,'new'),account_ref='bound'))
    assert activities(c,'component_recovered')==1 and live_ready(c)
    assert c.meta('native_session')['generation']==1
    health.ready(SimpleNamespace(identity=(2,2,'new'),account_ref='bound'))
    assert activities(c,'component_recovered')==1 and c.meta('native_session')['generation']==1
    assert c.meta('native_session')['active_root_count']==1
    c.close()


def test_process_replacement_and_same_process_relogin_increment_generation(tmp_path,monkeypatch):
    c=ControlStore(tmp_path);h=SessionHealth(c)
    reader=SimpleNamespace(identity=(1,1,'first'),account_ref='bound')
    h.ready(reader);assert c.meta('native_session')['generation']==1
    reader.identity=(2,2,'replacement');h.ready(reader)
    assert c.meta('native_session')['generation']==2
    monkeypatch.setattr(session_health,'diagnostics',lambda:{'process_identity':[2,2,'replacement'],'active_root_count':0})
    h.failed(NativeAccountUnavailable('no account'));h.ready(reader)
    assert c.meta('native_session')['generation']==3
    c.close()


def test_account_switch_stays_paused_and_diagnostics_do_not_leak_error_text(tmp_path,monkeypatch):
    c=ControlStore(tmp_path);c.set_meta('account',{'account_ref':'bound'})
    monkeypatch.setattr(session_health,'diagnostics',lambda:{'process_identity':[1,1,'start'],'active_root_count':1})
    h=SessionHealth(c);h.failed(RuntimeError('登录账号与绑定账号不同 PRIVATE-CHAT-CONTENT'))
    assert c.meta('native_session')['state']=='account_changed' and not live_ready(c)
    assert 'PRIVATE-CHAT-CONTENT' not in str(list(c.db.execute('SELECT body FROM activity')))
    assert 'PRIVATE-CHAT-CONTENT' not in str(c.meta('native_session'))
    c.close()


def test_sticker_retries_survive_restart_and_reset_after_success(tmp_path):
    c=ControlStore(tmp_path);gate=FailureGate(c,'stickers',(30,60,120,300))
    assert gate.fail(RuntimeError('no matching frame'),now=100)==30
    gate=FailureGate(c,'stickers',(30,60,120,300))
    assert not gate.due(129) and gate.due(130)
    assert gate.fail(RuntimeError('no matching frame'),now=130)==60
    assert activities(c,'component_fault')==1
    gate.success(now=200);assert gate.due(200)
    assert gate.fail(RuntimeError('again'),now=201)==30
    assert activities(c,'component_fault')==2
    c.close()


def test_missing_process_clears_stale_healthy_metadata(monkeypatch):
    import native_keys
    def gone():raise ProcessLookupError('gone')
    monkeypatch.setattr(native_keys,'process_identity',gone)
    assert session_health.diagnostics()=={'process_identity':None,'active_root_count':None,'process_error':'ProcessLookupError'}


def test_snapshot_contention_keeps_session_generation_and_emits_no_fault(tmp_path):
    c=ControlStore(tmp_path);c.set_meta('account',{'account_ref':'bound'})
    reader=SimpleNamespace(identity=(1,1,'start'),account_ref='bound',assert_active_account=lambda:None)
    h=SessionHealth(c);h.ready(reader);before=activities(c,'native_session')
    for _ in range(15):h.read_busy(reader)
    assert live_ready(c) and c.meta('native_session')['generation']==1
    assert c.meta('native_session')['snapshot_busy_count']==15
    assert activities(c,'native_session')==before and activities(c,'component_fault')==0
    assert h.failed(SnapshotChanged('WAL changed'))==.3
    assert live_ready(c) and activities(c,'component_fault')==0
    c.close()


def test_snapshot_race_does_not_hide_a_real_account_loss(tmp_path):
    c=ControlStore(tmp_path);h=SessionHealth(c)
    def unavailable():raise NativeAccountUnavailable('gone')
    reader=SimpleNamespace(assert_active_account=unavailable)
    with pytest.raises(NativeAccountUnavailable):h.read_busy(reader)
    assert not live_ready(c);c.close()


def test_alternating_wake_and_account_errors_do_not_reset_backoff(tmp_path,monkeypatch):
    c=ControlStore(tmp_path);h=SessionHealth(c)
    monkeypatch.setattr(session_health,'diagnostics',lambda:{'process_identity':[1,1,'same'],'active_root_count':0})
    errors=[NativeAccountUnavailable('none'),RuntimeError('wake failed')]*20
    delays=[h.failed(e) for e in errors]
    assert delays[:6]==[2,5,10,20,30,60] and delays[-1]==60
    assert c.meta('fault:native_session')['count']==40
    assert activities(c,'component_fault')==1 and activities(c,'native_session')==1
    assert c.meta('native_session')['state']=='unavailable'
    c.close()


def test_collector_retains_reader_and_requeues_ack_on_snapshot_race(tmp_path,monkeypatch):
    import queue
    import agent_worker
    c=ControlStore(tmp_path);c.set_meta('account',{'account_ref':'bound'})
    reader=SimpleNamespace(identity=(1,1,'start'),account_ref='bound',private_ids=frozenset(),assert_active_account=lambda:None)
    constructed=[];acked=[];delays=[]
    class Stop:
        done=False
        def is_set(self):return self.done
        def wait(self,delay):
            delays.append(delay)
            if len(delays)==2:self.done=True
    stop=Stop();acks=queue.Queue();acks.put(('chat','batch'))
    def acknowledge(*args):
        acked.append(args)
        if len(acked)==1:raise SnapshotChanged('concurrent native write')
    progress=SimpleNamespace(acknowledge=acknowledge,close=lambda:None)
    monkeypatch.setattr(agent_worker,'ControlStore',lambda _:c)
    monkeypatch.setattr(agent_worker,'ConsumerState',lambda _:progress)
    monkeypatch.setattr(agent_worker,'NativeReader',lambda **kwargs:(constructed.append(kwargs) or reader))
    monkeypatch.setattr(agent_worker,'run_ui',lambda *args:{'status':'verified'})
    monkeypatch.setattr(agent_worker,'directory',lambda *args:None)
    monkeypatch.setattr(agent_worker,'STOP',stop);monkeypatch.setattr(agent_worker,'ACK',acks)
    monkeypatch.setattr(c,'close',lambda:None)
    agent_worker.collect()
    assert len(constructed)==1 and len(acked)==2 and acks.empty() and delays[0]==.3
    assert c.meta('native_session')['generation']==1 and activities(c,'component_fault')==0
    c.db.close()


def test_offline_send_jobs_remain_queued_and_resume_after_verified_health(tmp_path):
    c=ControlStore(tmp_path);c.set_meta('account',{'account_ref':'bound'})
    job=c.enqueue('moments_publish',{'id':'synthetic'})
    assert c.take_job(exclude=session_health.blocked_send_jobs(c)) is None
    assert c.db.execute('SELECT state FROM jobs WHERE id=?',(job,)).fetchone()[0]=='queued'
    SessionHealth(c).ready(SimpleNamespace(identity=(1,1,'start'),account_ref='bound'))
    assert c.take_job(exclude=session_health.blocked_send_jobs(c))['id']==job
    c.close()


def load_node(filename,name,namespace):
    # Exercise actual UI methods with fake X windows, without a live display.
    tree=ast.parse((RUNTIME/filename).read_text())
    node=next(n for n in tree.body if getattr(n,'name',None)==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),filename,'exec'),namespace)
    return namespace[name]


@pytest.mark.parametrize('change',[
    {'account_ref':'other'}, {'wechat_pid':0}, {'start_ticks':'reused'},
])
def test_window_refresh_rejects_wrong_account_or_reused_process(change):
    verify=load_node('ui_session.py','verify_request',{})
    request={'wechat_pid':2,'account_ref':'bound','start_ticks':'start',**change}
    with pytest.raises(RuntimeError):verify(request,{'chat':{'account_ref':'bound'}},['1000']*4,'wechat','start')


def test_window_refresh_accepts_new_verified_process_but_not_mixed_accounts():
    verify=load_node('ui_session.py','verify_request',{})
    request={'wechat_pid':200,'account_ref':'bound','start_ticks':'new'}
    verify(request,{'chat':{'account_ref':'bound'}},['1000']*4,'wechat','new')
    with pytest.raises(RuntimeError):
        verify(request,{'a':{'account_ref':'bound'},'b':{'account_ref':'other'}},['1000']*4,'wechat','new')
