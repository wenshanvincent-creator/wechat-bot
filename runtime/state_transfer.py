"""Private application-state migration. Native login profiles/cursors are not copied."""
import argparse
import json
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path
from control_store import ControlStore

FILES={'control.sqlite','decisions.sqlite','context.sqlite','group-memory.sqlite'}
DIRECTORIES={'memories','sessions','personas','secrets','media','image-receipts'}


def export_state(root,target):
    root=Path(root);target=Path(target)
    if target.exists():raise ValueError('导出文件已存在，请使用新文件名')
    with tempfile.TemporaryDirectory() as temporary:
        stage=Path(temporary)/'state';stage.mkdir()
        for path in root.rglob('*'):
            relative=path.relative_to(root)
            if relative.parts[0] not in FILES|DIRECTORIES or path.is_symlink() or not path.is_file():continue
            if path.name.endswith(('-wal','-shm')):continue
            destination=stage/relative;destination.parent.mkdir(parents=True,exist_ok=True)
            if path.suffix=='.sqlite':
                source=sqlite3.connect(path);copy=sqlite3.connect(destination)
                try:source.backup(copy)
                finally:copy.close();source.close()
            else:shutil.copy2(path,destination)
        control=ControlStore(stage)
        control.set_meta('migration_exported_at',time.time())
        control.set_meta('settings',{**control.meta('settings',{}),'enabled':False,'agent_dialogue_enabled':False,'moments_enabled':False,'moments_auto_publish':False})
        control.db.execute('DELETE FROM auth');control.db.commit();control.close()
        with tarfile.open(target,'w:gz',compresslevel=3) as archive:archive.add(stage,arcname='state')
    target.chmod(0o600)
    return {'path':str(target),'private':True,'login_profile_included':False}


def import_state(root,source,password):
    root=Path(root)
    if (root/'control.sqlite').exists():raise ValueError('目标已有应用数据，请先停服务并备份，迁移到新数据卷')
    with tempfile.TemporaryDirectory() as temporary:
        stage=Path(temporary)
        with tarfile.open(source,'r:gz') as archive:archive.extractall(stage,filter='data')
        incoming=stage/'state'
        if not (incoming/'control.sqlite').is_file():raise ValueError('缺少应用状态数据库')
        for path in incoming.rglob('*'):
            relative=path.relative_to(incoming)
            if path.is_symlink() or relative.parts[0] not in FILES|DIRECTORIES:raise ValueError('迁移包包含非应用状态文件')
        root.mkdir(exist_ok=True,parents=True)
        for path in incoming.iterdir():
            if path.is_dir():shutil.copytree(path,root/path.name)
            else:shutil.copy2(path,root/path.name)
    control=ControlStore(root);account=control.meta('account',{})
    control.set_meta('account',{'self_sender_id':account.get('self_sender_id',''),'account_ref':''})
    control.set_meta('bindings',[]);control.set_meta('v2_live_ready',False)
    control.set_meta('runtime',{});control.set_meta('admin_runtime',{})
    control.set_meta('settings',{**control.meta('settings',{}),'enabled':False,'agent_dialogue_enabled':False,'moments_enabled':False,'moments_auto_publish':False})
    control.set_password(password)
    with control.db:
        control.db.execute('DELETE FROM auth')
        control.db.execute("UPDATE jobs SET state='interrupted',result=? WHERE state IN ('queued','running','approval','approved','running_host')",(json.dumps({'message':'服务器迁移前未结束的任务已隔离，未自动重试'}),))
        control.db.execute('DELETE FROM session_run')
        if control.db.execute("SELECT 1 FROM sqlite_master WHERE name='broker_tokens'").fetchone():control.db.execute('DELETE FROM broker_tokens')
        if control.db.execute("SELECT 1 FROM sqlite_master WHERE name='friend_requests'").fetchone():control.db.execute("UPDATE friend_requests SET state='stale'")
    control.set_meta('migration_imported_at',time.time());control.close()
    return {'imported':True,'automatic_chat_enabled':False,'wechat_login_required':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['export','import']);parser.add_argument('file');parser.add_argument('--state',default='/state');args=parser.parse_args()
    if args.action=='export':result=export_state(args.state,args.file)
    else:
        values=dict(line.split('=',1) for line in Path('/bot-config/secrets.env').read_text().splitlines() if '=' in line)
        result=import_state(args.state,args.file,values['CONSOLE_PASSWORD'])
    print(json.dumps(result))
