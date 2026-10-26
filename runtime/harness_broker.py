"""Short-lived task credential broker. API secrets never enter the tool sandbox."""
import hashlib
import http.client
import json
import os
import secrets
import socket
import socketserver
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from control_store import ControlStore
from public_web import fetch


class UnixConnection(http.client.HTTPConnection):
    def __init__(self,path,timeout=180):super().__init__('localhost',timeout=timeout);self.path=str(path)
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(self.timeout);self.sock.connect(self.path)


def call(path,route,body,token='',timeout=180):
    c=UnixConnection(path,timeout)
    try:
        c.request('POST',route,json.dumps(body,ensure_ascii=False).encode(),{'Content-Type':'application/json','Authorization':'Bearer '+token})
        r=c.getresponse();value=json.load(r)
        if r.status!=200:raise RuntimeError(value.get('error','Harness 任务失败'))
        return value
    finally:c.close()


def issue(control,provider,seconds=300):
    control.db.execute('CREATE TABLE IF NOT EXISTS broker_tokens(hash TEXT PRIMARY KEY,provider TEXT,expires REAL)')
    if 'remaining' not in {r[1] for r in control.db.execute('PRAGMA table_info(broker_tokens)')}:
        control.db.execute('ALTER TABLE broker_tokens ADD COLUMN remaining INTEGER DEFAULT 60');control.db.commit()
    token=secrets.token_urlsafe(32)
    with control.db:
        control.db.execute('DELETE FROM broker_tokens WHERE expires<?',(time.time(),))
        control.db.execute('INSERT INTO broker_tokens(hash,provider,expires,remaining) VALUES (?,?,?,60)',(hashlib.sha256(token.encode()).hexdigest(),provider,time.time()+seconds))
    return token


def revoke(control,token):
    with control.db:control.db.execute('DELETE FROM broker_tokens WHERE hash=?',(hashlib.sha256(token.encode()).hexdigest(),))


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        store=ControlStore('/state')
        try:
            token=self.headers.get('Authorization','').removeprefix('Bearer ')
            digest=hashlib.sha256(token.encode()).hexdigest()
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                row=store.db.execute('SELECT provider FROM broker_tokens WHERE hash=? AND expires>? AND remaining>0',(digest,time.time())).fetchone()
                if row:store.db.execute('UPDATE broker_tokens SET remaining=remaining-1 WHERE hash=?',(digest,))
            if not row:raise PermissionError('工具任务凭据无效或已过期')
            size=int(self.headers.get('Content-Length',0))
            if not 0<size<=8*1024*1024:raise ValueError('工具请求过大')
            body=json.loads(self.rfile.read(size))
            if self.path=='/fetch':
                value=fetch(body['url']);data=json.dumps(value,ensure_ascii=False).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            if self.path not in ('/v1/chat/completions','/v1/messages'):raise ValueError('工具代理接口不存在')
            provider=store.get('providers',row['provider'])
            if body.get('model')!=provider['model']:raise ValueError('此任务不能切换模型')
            if type(body.get('max_tokens')) is not int or not 1<=body['max_tokens']<=4096:raise ValueError('工具输出预算超出此任务权限')
            base=provider['base_url'].rstrip('/')
            if self.path=='/v1/messages':
                if base=='https://api.deepseek.com':base+='/anthropic'
                url=base+('/messages' if base.endswith('/v1') else '/v1/messages')
                headers={'Content-Type':'application/json','x-api-key':store.secret(provider['id'])}
                for field in ('anthropic-version','anthropic-beta'):
                    if self.headers.get(field):headers[field]=self.headers[field]
            else:url=base+'/chat/completions';headers={'Content-Type':'application/json','Authorization':'Bearer '+store.secret(provider['id'])}
            request=urllib.request.Request(url,data=json.dumps(body,ensure_ascii=False).encode(),headers=headers)
            with urllib.request.urlopen(request,timeout=90) as response:
                self.send_response(200);self.send_header('Content-Type',response.headers.get('Content-Type','application/json'));self.end_headers()
                while chunk:=response.read(8192):self.wfile.write(chunk);self.wfile.flush()
        except Exception as error:
            # Do not print upstream bodies, headers, prompts or token material.
            data=json.dumps({'error':str(error) if isinstance(error,(ValueError,PermissionError)) else type(error).__name__}).encode()
            self.send_response(400);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
        finally:store.close()


class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):daemon_threads=True


def start(path='/broker/model.sock'):
    path=Path(path);path.parent.mkdir(exist_ok=True)
    if path.exists():path.unlink()
    server=Server(str(path),Handler);os.chmod(path,0o666)
    threading.Thread(target=server.serve_forever,daemon=True).start();return server
