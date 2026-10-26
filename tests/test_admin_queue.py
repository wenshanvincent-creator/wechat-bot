import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from control_store import ControlStore
from admin_queue import claim,finish


def test_host_executor_only_claims_operator_approved_exact_commands(tmp_path):
    store=ControlStore(tmp_path)
    key=store.enqueue('admin_command',{'command':'echo exact-reviewable-command','reason':'test'})
    assert claim(store,'host')['job'] is None
    store.finish_job(key,'approval',{'message':'待审批'})
    assert claim(store,'host')['job'] is None
    store.finish_job(key,'approved',{'message':'已批准'})
    job=claim(store,'host')['job'];assert job['body']['command']=='echo exact-reviewable-command'
    assert claim(store,'another-host')['job'] is None
    body=dict(job_id=key,claim=job['claim'],executor_id='host',result={'returncode':0,'stdout':'ok'})
    with pytest.raises(PermissionError):finish(store,{**body,'claim':'wrong'})
    finish(store,body);assert store.jobs()[0]['state']=='done'
    assert finish(store,body)['already_recorded']
