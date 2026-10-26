"""Small persistent control plane. Credentials never enter public config JSON."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

KINDS={'providers','personas','sessions','channels'}


def active_persona(session):
    return session.get('active_persona_id') or session['persona_ids'][0]


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',value):
        raise ValueError('无效的记录 ID')
    return value


class ControlStore:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.root/'control.sqlite',timeout=10)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS records(kind TEXT,id TEXT,body TEXT,updated REAL,PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS auth(token_hash TEXT PRIMARY KEY,csrf TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,body TEXT,state TEXT,result TEXT,created REAL,updated REAL);
            CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY,kind TEXT,body TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS human_events(id TEXT PRIMARY KEY,session_id TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS session_run(id TEXT PRIMARY KEY,epoch INTEGER DEFAULT 0,bot_count INTEGER DEFAULT 0,next_agent INTEGER DEFAULT 0,pending_at REAL,channel_id TEXT,tried TEXT DEFAULT '[]');
        ''')

    def close(self):self.db.close()

    def meta(self,key,default=None):
        row=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self,key,value):
        with self.db:self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))

    def list(self,kind):
        return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM records WHERE kind=? ORDER BY updated,id',(kind,))]

    def get(self,kind,key):
        identifier(key)
        row=self.db.execute('SELECT body FROM records WHERE kind=? AND id=?',(kind,key)).fetchone()
        if not row:raise KeyError('记录不存在')
        return json.loads(row[0])

    def secret(self,provider):
        path=self.root/'secrets'/(identifier(provider)+'.key')
        return path.read_text().strip() if path.exists() else ''

    def set_secret(self,provider,value):
        if not isinstance(value,str):raise ValueError('密钥必须是文本')
        root=self.root/'secrets';root.mkdir(mode=0o700,exist_ok=True)
        path=root/(identifier(provider)+'.key');temp=path.with_suffix('.tmp')
        with os.fdopen(os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as output:
            output.write(value.strip());output.flush();os.fsync(output.fileno())
        temp.replace(path)

    def put(self,kind,value):
        if kind not in KINDS:raise ValueError('未知配置类型')
        value=dict(value);key=identifier(value.get('id') or uuid.uuid4().hex);new_secret=None
        value['id']=key
        if not isinstance(value.get('name'),str) or not value['name'].strip():raise ValueError('请填写名称')
        value['name']=value['name'].strip()
        if kind=='providers':
            allowed={'id','name','base_url','model','context_window_tokens','max_output_tokens','vision','tools','json_mode','thinking','protocol','api_key','has_key'}
            from urllib.parse import urlsplit
            url=urlsplit(value.get('base_url',''))
            if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:raise ValueError('API 地址必须是无内嵌凭据的 HTTP(S) 地址')
            if not value.get('model'):raise ValueError('请填写实际 model ID')
            if value.get('protocol','openai-completions')!='openai-completions':raise ValueError('此接口目前支持 OpenAI Chat Completions 协议')
            for field,default in [('context_window_tokens',1000000),('max_output_tokens',4096)]:
                number=value.get(field,default)
                if type(number) is not int or number<256:raise ValueError('模型容量必须是至少 256 的整数')
                value[field]=number
            if value.get('api_key'):new_secret=value['api_key']
            value.pop('api_key',None);value.pop('has_key',None)
        elif kind=='personas':
            allowed={'id','name','provider_id','persona','enabled','tool_access','moments_access'}
            self.get('providers',value.get('provider_id',''))
            if not isinstance(value.get('persona'),str) or not value['persona'].strip():raise ValueError('请填写人设内容')
        elif kind=='sessions':
            allowed={'id','name','persona_ids','active_persona_id','max_bot_messages_without_human','compression_ratio','enabled'}
            if not isinstance(value.get('persona_ids'),list) or not value['persona_ids']:raise ValueError('会话至少选择一个人设')
            if len(set(value['persona_ids']))!=len(value['persona_ids']):raise ValueError('人设不能重复')
            for persona in value['persona_ids']:self.get('personas',persona)
            value['active_persona_id']=active_persona(value)
            if value['active_persona_id'] not in value['persona_ids']:raise ValueError('当前人设必须属于这个会话')
            limit=value.get('max_bot_messages_without_human',6)
            if type(limit) is not int or limit<1:raise ValueError('无人类参与的消息上限至少为 1')
            ratio=value.get('compression_ratio',.5)
            if not isinstance(ratio,(int,float)) or not 0<ratio<1:raise ValueError('压缩比例应在 0 和 1 之间')
        else:
            allowed={'id','name','native_id','kind','session_id','enabled','allow_read','allow_send','allow_proactive','proactive_interval_minutes','proactive_max_bubbles','quiet_start','quiet_end','proactive_topic','aliases'}
            self.get('sessions',value.get('session_id',''))
            if value.get('kind') not in ('group','friend'):raise ValueError('频道必须是群聊或好友')
            if not isinstance(value.get('native_id'),str) or not value['native_id']:raise ValueError('请从微信目录选择一个聊天')
            if value.get('kind')=='group' and not value['native_id'].endswith('@chatroom'):raise ValueError('这不是群聊 ID')
            if value.get('allow_proactive') and not value.get('allow_send'):raise ValueError('主动开聊需要先允许发送')
            aliases=value.get('aliases',[])
            if not isinstance(aliases,list) or len(aliases)>6 or any(not isinstance(a,str) or not 1<=len(a.strip())<=20 for a in aliases):raise ValueError('别名最多 6 个，每个 1–20 字')
            value['aliases']=[a.strip() for a in aliases]
            bubbles=value.get('proactive_max_bubbles',2)
            if type(bubbles) is not int or not 1<=bubbles<=6:raise ValueError('主动开聊每轮气泡数请填 1–6')
            interval=value.get('proactive_interval_minutes',360)
            if type(interval) is not int or interval<1:raise ValueError('主动开聊检查间隔至少一分钟')
            for field in ('quiet_start','quiet_end'):
                if value.get(field) and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value[field]):raise ValueError('安静时段请用 HH:MM')
            for old in self.list('channels'):
                if old['id']!=key and old['native_id']==value['native_id']:raise ValueError('该聊天已配置，请编辑现有绑定')
        if set(value)-allowed:raise ValueError('配置包含未知字段：'+','.join(sorted(set(value)-allowed)))
        if new_secret is not None:self.set_secret(key,new_secret)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?)',(kind,key,json.dumps(value,ensure_ascii=False),time.time()))
            if kind=='sessions':
                # Cancel an in-flight generation/continuation when its role changes.
                self.db.execute('UPDATE session_run SET epoch=epoch+1,pending_at=NULL WHERE id=?',(key,))
        if kind=='personas':
            folder=self.root/'personas'/key;folder.mkdir(parents=True,exist_ok=True)
            (folder/'persona.md').write_text(value['persona'],encoding='utf-8')
        self.set_meta('revision',self.meta('revision',0)+1)
        return self.public(kind,value)

    def public(self,kind,value):
        value=dict(value)
        if kind=='providers':value['has_key']=bool(self.secret(value['id']))
        return value

    def delete(self,kind,key):
        self.get(kind,key)
        references=[]
        if kind=='providers':references=[p['name'] for p in self.list('personas') if p['provider_id']==key]
        elif kind=='personas':references=[s['name'] for s in self.list('sessions') if key in s['persona_ids']]
        elif kind=='sessions':references=[c['name'] for c in self.list('channels') if c['session_id']==key]
        if references:raise ValueError('仍被使用：'+'、'.join(references))
        with self.db:self.db.execute('DELETE FROM records WHERE kind=? AND id=?',(kind,key))
        self.set_meta('revision',self.meta('revision',0)+1)
        # Archive semantics: files stay on disk until an explicit data purge.

    def memory_path(self,persona,session):
        self.get('personas',persona);self.get('sessions',session)
        folder=self.root/'memories'/identifier(persona)/identifier(session)
        folder.mkdir(parents=True,exist_ok=True)
        return folder/'memory.sqlite'

    def conversation_path(self,session):
        self.get('sessions',session)
        folder=self.root/'sessions'/identifier(session);folder.mkdir(parents=True,exist_ok=True)
        return folder/'conversation.sqlite'

    def seed(self,legacy,persona,key=''):
        if self.list('providers'):return
        self.put('providers',dict(id='deepseek',name='DeepSeek V4.1 Flash 1017',base_url='https://api.deepseek.com',model='deepseek-flash',context_window_tokens=1000000,max_output_tokens=4096,vision=True,tools=True,json_mode=True,thinking=False,protocol='openai-completions',api_key=key))
        self.put('personas',dict(id='whale',name='鲸鱼娘',provider_id='deepseek',persona=persona,enabled=True,tool_access=True,moments_access=True))
        self.put('sessions',dict(id='main',name='主会话',persona_ids=['whale'],max_bot_messages_without_human=6,compression_ratio=.5,enabled=True))
        for index,group in enumerate(legacy.get('groups',[])):
            self.put('channels',dict(id='local-'+str(index),name='本地测试群',native_id=group,kind='group',session_id='main',enabled=True,allow_read=True,allow_send=True,allow_proactive=False,proactive_interval_minutes=360,quiet_start='23:00',quiet_end='08:00',proactive_topic=''))
        self.set_meta('settings',dict(enabled=False,agent_dialogue_enabled=False,timezone='Asia/Shanghai',poll_seconds=3,debounce_ms=1500,vision_provider_id='deepseek',memory_provider_id='deepseek',moments_enabled=False))

    def set_password(self,password):
        if len(password)<12:raise ValueError('管理密码至少 12 个字符')
        salt=secrets.token_hex(16)
        self.set_meta('admin_password',{'salt':salt,'hash':hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),200000).hex()})

    def login(self,password):
        record=self.meta('admin_password')
        if not record or not hmac.compare_digest(record['hash'],hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(record['salt']),200000).hex()):raise PermissionError('密码不正确')
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24)
        with self.db:self.db.execute('INSERT INTO auth VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,time.time()+12*3600))
        return token,csrf

    def authenticate(self,token,csrf=None):
        row=self.db.execute('SELECT csrf,expires FROM auth WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row['expires']<time.time() or (csrf is not None and not hmac.compare_digest(csrf,row['csrf'])):raise PermissionError('请重新登录')
        return row['csrf']

    def enqueue(self,kind,body):
        key=uuid.uuid4().hex;now=time.time()
        with self.db:self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',(key,kind,json.dumps(body,ensure_ascii=False),'queued',None,now,now))
        return key

    def jobs(self):
        return [dict(id=r['id'],kind=r['kind'],body=json.loads(r['body']),state=r['state'],result=json.loads(r['result']) if r['result'] else None,created=r['created'],updated=r['updated']) for r in self.db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100')]

    def take_job(self,exclude=()):
        # A stray DML statement may have opened an implicit transaction; close
        # it so BEGIN IMMEDIATE does not fail and crash the worker loop.
        if self.db.in_transaction:self.db.commit()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            query="SELECT * FROM jobs WHERE state='queued'"
            if exclude:query+=' AND kind NOT IN ('+','.join('?' for _ in exclude)+')'
            row=self.db.execute(query+' ORDER BY created LIMIT 1',tuple(exclude)).fetchone()
            if row:self.db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?",(time.time(),row['id']))
        return dict(row) if row else None

    def finish_job(self,key,state,result):
        with self.db:self.db.execute('UPDATE jobs SET state=?,result=?,updated=? WHERE id=?',(state,json.dumps(result,ensure_ascii=False),time.time(),key))

    def activity(self,kind,body):
        with self.db:self.db.execute('INSERT INTO activity(kind,body,created) VALUES (?,?,?)',(kind,json.dumps(body,ensure_ascii=False),time.time()))

    def snapshot(self):
        from moments import Moments
        media=[];friend_requests=[]
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='friend_requests'").fetchone():
            friend_requests=[dict(request_id=r['id'],name=r['name'],expires_at=r['expires']) for r in self.db.execute("SELECT id,name,expires FROM friend_requests WHERE state='pending' AND expires>? ORDER BY expires DESC",(time.time(),))]
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='media_events'").fetchone():
            media=[dict(id=r['id'],channel_id=r['channel_id'],state=r['state'],result=json.loads(r['result']) if r['result'] else None,updated=r['updated']) for r in self.db.execute('SELECT * FROM media_events ORDER BY updated DESC LIMIT 100')]
        wechat={'account_bound':bool(self.meta('account',{}).get('account_ref')),'multi_account_supported':False,'agent_dialogue_enabled':False}
        receipts=[]
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='decisions'").fetchone():
            names={c['native_id']:c['name'] for c in self.list('channels')}
            receipts=[dict(group_id=r['group_id'],name=names.get(r['group_id'],r['group_id']),state=r['state'],
                           created=r['created'],text=r['text'])
                      for r in self.db.execute("SELECT group_id,state,created,text FROM decisions WHERE state IN ('claimed','uncertain') ORDER BY created DESC LIMIT 20")]
        import classroom
        notices=classroom.list_notices(self)
        reminders=classroom.list_reminders(self)
        import timetable as schedule
        timetable={'entries':schedule.entries(self),'settings':schedule.settings(self)}
        return {**{k:[self.public(k,v) for v in self.list(k)] for k in KINDS},'wechat':wechat,'friend_requests':friend_requests,'receipts':receipts,'revision':self.meta('revision',0),'settings':self.meta('settings',{}),'runtime':self.meta('runtime',{}),'admin_runtime':self.meta('admin_runtime',{}),'bindings':self.meta('bindings',[]),'directory':self.meta('directory',[]),'media':media,'moments':Moments(self).list(),'notices':notices,'reminders':reminders,'timetable':timetable,'jobs':self.jobs(),'activity':[dict(kind=r['kind'],body=json.loads(r['body']),created=r['created']) for r in self.db.execute('SELECT * FROM activity ORDER BY id DESC LIMIT 80')]}
