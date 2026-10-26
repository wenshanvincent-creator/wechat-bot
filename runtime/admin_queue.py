"""Claim only exact commands already approved by the console operator."""
import json
import time
import uuid


def claim(store,executor):
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        row=store.db.execute("SELECT * FROM jobs WHERE kind='admin_command' AND state='approved' ORDER BY created LIMIT 1").fetchone()
        if not row:return {'job':None}
        receipt={'executor_id':executor,'claim':uuid.uuid4().hex,'claimed_at':time.time()}
        store.db.execute("UPDATE jobs SET state='running_host',result=?,updated=? WHERE id=?",(json.dumps(receipt),time.time(),row['id']))
    return {'job':{'id':row['id'],'body':json.loads(row['body']),**receipt}}


def finish(store,body):
    row=store.db.execute('SELECT * FROM jobs WHERE id=?',(body['job_id'],)).fetchone()
    if row and row['state'] in ('done','failed','uncertain'):
        old=json.loads(row['result'])
        if old.get('claim')==body.get('claim') and old.get('executor_id')==body.get('executor_id') and all(old.get(k)==v for k,v in body['result'].items()):return {'ok':True,'already_recorded':True}
    if not row or row['state']!='running_host':raise ValueError('任务没有被管理员执行器领取')
    claim=json.loads(row['result'])
    if body.get('claim')!=claim['claim'] or body.get('executor_id')!=claim['executor_id']:raise PermissionError('执行器回执不匹配')
    result=body['result']
    if not isinstance(result.get('returncode'),int):raise ValueError('缺少进程退出码')
    state='done' if result['returncode']==0 else 'uncertain' if result['returncode']==125 else 'failed'
    result={**result,'message':'管理员命令执行完成' if state=='done' else '上次执行中断，结果需核对，未重试' if state=='uncertain' else '管理员命令执行失败','executor_id':claim['executor_id'],'claim':claim['claim']}
    store.finish_job(row['id'],state,result)
    return {'ok':True}
