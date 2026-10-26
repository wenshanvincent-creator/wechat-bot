import json
import sys
import tarfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from control_store import ControlStore
from state_transfer import export_state,import_state


def test_private_migration_keeps_memories_but_not_login_cursors_or_live_permissions(tmp_path):
    source=tmp_path/'source';store=ControlStore(source);store.seed({'groups':[]},'角色','private-model-key');store.set_password('old-admin-password')
    store.set_meta('settings',{**store.meta('settings'),'enabled':True,'moments_enabled':True});store.set_meta('account',{'account_ref':'old-cache','self_sender_id':'wxid_same'})
    job=store.enqueue('admin_command',{'command':'must-not-run'});store.finish_job(job,'approved',{})
    store.db.execute('CREATE TABLE friend_requests(id TEXT PRIMARY KEY,name TEXT,row_hash TEXT,account TEXT,expires REAL,state TEXT)')
    store.db.execute("INSERT INTO friend_requests VALUES ('request','申请人','old-window-row','old-cache',9999999999,'pending')");store.db.commit()
    folder=source/'memories'/'whale'/'main';folder.mkdir(parents=True);(folder/'receipt.txt').write_text('memory-evidence')
    ui=source/'ui';ui.mkdir();(ui/'conversations.json').write_text('old-window')
    (source/'consumer-v2.sqlite').write_bytes(b'old-native-cursor')
    target=tmp_path/'private.tar.gz';export_state(source,target)
    with tarfile.open(target) as archive:
        names=archive.getnames();assert not any('/ui/' in p or 'consumer-v2' in p for p in names)
    destination=tmp_path/'destination';import_state(destination,target,'new-admin-password')
    imported=ControlStore(destination)
    assert imported.secret('deepseek')=='private-model-key'
    assert not imported.meta('settings')['enabled'] and not imported.meta('settings')['moments_enabled']
    assert imported.meta('account')=={'account_ref':'','self_sender_id':'wxid_same'}
    assert imported.jobs()[0]['state']=='interrupted'
    assert imported.db.execute('SELECT state FROM friend_requests').fetchone()[0]=='stale'
    assert (destination/'memories'/'whale'/'main'/'receipt.txt').read_text()=='memory-evidence'
    imported.login('new-admin-password')
