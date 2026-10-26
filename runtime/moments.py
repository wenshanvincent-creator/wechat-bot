"""Durable drafts and at-most-once public submission, separate from chat ACLs."""
import json
import time
import uuid


class Moments:
    def __init__(self,control):
        self.control=control;self.db=control.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS moments(
            id TEXT PRIMARY KEY,body TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT,created REAL,updated REAL)''');self.db.commit()

    def save(self,text,visibility='private',persona_id=None):
        if not isinstance(text,str) or not text.strip() or len(text)>2000:raise ValueError('朋友圈文字必须为 1–2000 字')
        if visibility not in ('private','friends'):raise ValueError('请选择明确可见范围')
        if persona_id and not self.control.get('personas',persona_id).get('moments_access'):raise PermissionError('此人设没有朋友圈权限')
        key=uuid.uuid4().hex;now=time.time()
        body={'text':text.strip(),'visibility':visibility,'persona_id':persona_id}
        with self.db:self.db.execute('INSERT INTO moments VALUES (?,?,?,?,?,?)',(key,json.dumps(body,ensure_ascii=False),'draft',None,now,now))
        return {'id':key,'state':'draft',**body}

    def list(self):
        return [dict(id=r['id'],state=r['state'],**json.loads(r['body']),receipt=json.loads(r['receipt']) if r['receipt'] else None,created=r['created']) for r in self.db.execute('SELECT * FROM moments ORDER BY created DESC LIMIT 100')]

    def publish(self,key,ui,prepare_only=False):
        row=self.db.execute('SELECT * FROM moments WHERE id=?',(key,)).fetchone()
        if not row:raise ValueError('草稿不存在')
        if row['state']!='draft':raise ValueError('该草稿已有提交记录，不自动重发')
        body=json.loads(row['body'])
        if not prepare_only and not self.control.meta('settings',{}).get('moments_enabled'):raise PermissionError('朋友圈发布尚未开启')
        if body.get('persona_id') and not self.control.get('personas',body['persona_id']).get('moments_access'):raise PermissionError('此人设的朋友圈权限已关闭')
        request={**body,'action':'prepare' if prepare_only else 'publish','operation_id':key,
                 'account_ref':self.control.meta('account',{})['account_ref']}
        # Claim before crossing the UI boundary. A crash after Return cannot repeat a post.
        if not prepare_only:
            with self.db:
                changed=self.db.execute("UPDATE moments SET state='claimed',updated=? WHERE id=? AND state='draft'",(time.time(),key)).rowcount
                if not changed:raise ValueError('这条草稿正在提交')
        try:receipt=ui('/bot/moments_ui.py',request)
        except Exception:
            if not prepare_only:
                with self.db:self.db.execute("UPDATE moments SET state='uncertain',updated=? WHERE id=?",(time.time(),key))
            raise
        if not prepare_only:
            state=receipt.get('status','uncertain')
            # UI submission alone is not proof that the server published the post.
            if state not in ('submitted','failed','uncertain'):state='uncertain'
            with self.db:self.db.execute('UPDATE moments SET state=?,receipt=?,updated=? WHERE id=?',(state,json.dumps(receipt,ensure_ascii=False),time.time(),key))
        return receipt
