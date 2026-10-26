"""Authenticated local/admin console. No additional framework or local model."""
import argparse
import json
import mimetypes
import os
import time
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote,urlsplit,parse_qs

from control_store import ControlStore,KINDS,identifier
from provider_client import ProviderClient

WEB=Path(__file__).resolve().parent/'console'
LOGIN_ATTEMPTS={};LOGIN_LOCK=threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def send_json(self,status,value,cookie=None):
        data=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        if cookie:self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

    def body(self):
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=1024*1024:raise ValueError('请求大小无效')
        if self.headers.get_content_type()!='application/json':raise ValueError('仅接收 JSON')
        return json.loads(self.rfile.read(size))

    def auth(self,store,mutate=False):
        cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''))
        token=cookie['wa_session'].value if 'wa_session' in cookie else ''
        if mutate:
            origin=self.headers.get('Origin')
            if origin and urlsplit(origin).netloc!=self.headers.get('Host'):raise PermissionError('来源不匹配')
        return store.authenticate(token,self.headers.get('X-CSRF-Token','') if mutate else None)

    def do_GET(self):
        store=ControlStore(self.server.state_root)
        try:
            path=unquote(urlsplit(self.path).path)
            if path.startswith('/api/'):
                csrf=self.auth(store)
                if path=='/api/state':self.send_json(200,{**store.snapshot(),'csrf':csrf})
                elif path=='/api/desktop.png':
                    folder=store.root/'console-preview';file=folder/'desktop.png'
                    if not file.exists():self.send_json(404,{'error':'尚无绑定预览'});return
                    if file.resolve().parent!=folder.resolve():raise ValueError('预览文件路径无效')
                    data=file.read_bytes();self.send_response(200);self.send_header('Content-Type','image/png');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                elif path.startswith('/api/sticker/'):
                    from sticker_catalog import catalog,resolve
                    digest=path.rsplit('/',1)[-1].removesuffix('.png')
                    selected=next((s for s in catalog() if s['sha256']==digest),None)
                    if not selected:self.send_json(404,{'error':'表情不存在'});return
                    _,file=resolve(selected['name']);data=file.read_bytes()
                    self.send_response(200);self.send_header('Content-Type','image/png');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                elif path.startswith('/api/memory/'):
                    _,_,_,persona,session=path.split('/')
                    from group_memory import GroupMemory
                    memory=GroupMemory(store.memory_path(persona,session),'shared')
                    self.send_json(200,{'facts':memory.all(session)});memory.db.close()
                elif path.startswith('/api/archive/'):
                    _,_,_,persona,session=path.split('/')
                    from episodic_memory import EpisodicMemory
                    memory=EpisodicMemory(store,persona,session);query=parse_qs(urlsplit(self.path).query)
                    try:self.send_json(200,{**memory.overview(query.get('day',[None])[0]),**memory.search(query.get('query',[''])[0],query.get('day',[None])[0],20)})
                    finally:memory.close()
                else:self.send_json(404,{'error':'接口不存在'})
                return
            relative='index.html' if path=='/' else path.lstrip('/')
            file=(WEB/relative).resolve()
            if not file.is_relative_to(WEB.resolve()) or not file.is_file():self.send_error(404);return
            data=file.read_bytes();self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(file)[0] or 'application/octet-stream')
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        except PermissionError as error:self.send_json(401,{'error':str(error)})
        except (ValueError,KeyError) as error:self.send_json(400,{'error':str(error)})
        finally:store.close()

    def do_POST(self):self.mutate('POST')
    def do_DELETE(self):self.mutate('DELETE')

    def mutate(self,method):
        store=ControlStore(self.server.state_root)
        try:
            path=unquote(urlsplit(self.path).path)
            if path=='/api/login':
                origin=self.headers.get('Origin')
                if origin and urlsplit(origin).netloc!=self.headers.get('Host'):raise PermissionError('来源不匹配')
                peer=self.client_address[0];now=time.time()
                with LOGIN_LOCK:
                    failures=[t for t in LOGIN_ATTEMPTS.get(peer,[]) if now-t<60]
                    LOGIN_ATTEMPTS[peer]=failures
                    if len(failures)>=5:
                        self.send_json(429,{'error':'登录失败过多，请一分钟后重试'});return
                try:token,csrf=store.login(self.body().get('password',''))
                except PermissionError:
                    with LOGIN_LOCK:LOGIN_ATTEMPTS[peer].append(now)
                    raise
                with LOGIN_LOCK:LOGIN_ATTEMPTS.pop(peer,None)
                secure='; Secure' if self.headers.get('X-Forwarded-Proto')=='https' else ''
                self.send_json(200,{'csrf':csrf},'wa_session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'+secure);return
            self.auth(store,True)
            pieces=path.strip('/').split('/')
            if path=='/api/admin/claim':
                from admin_queue import claim
                result=claim(store,self.body()['executor_id'])
            elif path=='/api/admin/heartbeat':
                value=self.body();store.set_meta('admin_runtime',{'executor_id':value['executor_id'],'heartbeat':time.time()});result={'ok':True}
            elif path=='/api/admin/finish':
                from admin_queue import finish
                result=finish(store,self.body())
            elif len(pieces)>=2 and pieces[0]=='api' and pieces[1] in KINDS:
                kind=pieces[1]
                if method=='DELETE':store.delete(kind,pieces[2]);result={'ok':True}
                else:result=store.put(kind,self.body())
            elif path=='/api/receipts/resolve':
                value=self.body()
                group=value.get('group_id');outcome=value.get('outcome')
                if not isinstance(group,str) or not group:raise ValueError('缺少聊天')
                if outcome not in ('sent','failed'):raise ValueError('请选择已发送或未发送')
                with store.db:
                    changed=store.db.execute("UPDATE decisions SET state=? WHERE group_id=? AND state IN ('claimed','uncertain')",(outcome,group)).rowcount
                result={'resolved':changed,'outcome':outcome}
            elif path=='/api/classroom/reminder/cancel':
                import classroom
                result=classroom.cancel_reminder(store,(self.body().get('id') or '').strip())
            elif path=='/api/classroom/notice/archive':
                import classroom
                result=classroom.update_notice(store,(self.body().get('id') or '').strip(),status='done')
            elif path=='/api/timetable/settings':
                import timetable
                value=self.body();allowed={'enabled','lead_minutes','channel_id'}
                if set(value)-allowed:raise ValueError('设置包含未知字段')
                result=timetable.configure(store,**value)
            elif path=='/api/timetable/import':
                import timetable
                items,errors=timetable.parse_text(self.body().get('text',''))
                if not items:raise ValueError('没有解析出课程；每行写成“周一 08:00-08:40 TOK 209”')
                timetable.replace(store,items)
                result={'count':len(items),'errors':errors[:10]}
            elif path=='/api/settings':
                value=self.body();allowed={'enabled','agent_dialogue_enabled','timezone','poll_seconds','debounce_ms','vision_provider_id','memory_provider_id','harness_provider_id','moments_enabled','moments_auto_publish','moments_interval_minutes','moments_visibility'}
                if set(value)-allowed:raise ValueError('设置包含未知字段')
                if value.get('agent_dialogue_enabled'):raise ValueError('当前只有一个微信实例，暂不支持多账号群内互聊')
                from zoneinfo import ZoneInfo
                ZoneInfo(value.get('timezone','Asia/Shanghai'))
                if not 1<=value.get('poll_seconds',3)<=60 or not 0<=value.get('debounce_ms',1500)<=10000:raise ValueError('轮询/合并间隔超出范围')
                if type(value.get('moments_interval_minutes',360)) is not int or value.get('moments_interval_minutes',360)<1:raise ValueError('自主朋友圈检查间隔至少一分钟')
                if value.get('moments_visibility','private') not in ('private','friends'):raise ValueError('朋友圈可见范围无效')
                for field in ('vision_provider_id','memory_provider_id','harness_provider_id'):
                    if field in value:store.get('providers',value[field])
                store.set_meta('settings',{**store.meta('settings',{}),**value});result=store.meta('settings')
            elif path=='/api/provider-test':
                provider=store.get('providers',self.body()['id']);result=ProviderClient(provider,store.secret(provider['id'])).test()
                store.set_meta('provider-test:'+provider['id'],{**result,'checked_at':time.time()})
            elif path=='/api/jobs':
                value=self.body()
                if value.get('kind') not in ('directory','enroll_account','bind','simulate','tool','vision','moments_draft','moments_publish','cancel','approval','memory_delete','memory_journal','friend_requests','friend_add','friend_accept','say_in'):raise ValueError('未知任务')
                result={'id':store.enqueue(value['kind'],value.get('body',{}))}
            else:self.send_json(404,{'error':'接口不存在'});return
            self.send_json(200,result)
        except PermissionError as error:self.send_json(401,{'error':str(error)})
        except (ValueError,KeyError,RuntimeError) as error:self.send_json(400,{'error':str(error)})
        except (OSError,TimeoutError):self.send_json(502,{'error':'上游 API 连接失败，请检查地址与网络'})
        finally:store.close()


def serve(root,host='127.0.0.1',port=13332):
    server=ThreadingHTTPServer((host,port),Handler);server.state_root=Path(root)
    server.serve_forever()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--state',default='/state');parser.add_argument('--host',default='0.0.0.0');parser.add_argument('--port',type=int,default=8090)
    args=parser.parse_args();os.umask(0o077)
    store=ControlStore(args.state)
    config=Path('/bot-config/config.json');persona=Path('/bot-config/personality.md');env=Path('/bot-config/secrets.env')
    values=dict(line.split('=',1) for line in env.read_text().splitlines() if '=' in line)
    store.seed(json.loads(config.read_text()),persona.read_text(),values.get('DEEPSEEK_API_KEY',''))
    if not store.meta('admin_password'):
        store.set_password(values['CONSOLE_PASSWORD'])
    store.close();serve(args.state,args.host,args.port)
