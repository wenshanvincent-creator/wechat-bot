"""Networkless unprivileged SDK runtime. Only workspace and broker socket are mounted."""
import json
import os
import socketserver
import threading
import signal
import subprocess
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from harness_broker import UnixConnection

BUSY=threading.Lock()
ACTIVE={};ACTIVE_LOCK=threading.Lock()


def terminate(process):
    try:os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError:return
    try:process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass


class Proxy(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        connection=UnixConnection('/broker/model.sock',timeout=100)
        try:
            size=int(self.headers.get('Content-Length',0))
            if not 0<size<=8*1024*1024:raise ValueError('request size')
            token=self.headers.get('x-api-key')
            headers={'Authorization':'Bearer '+token if token else self.headers.get('Authorization',''),'Content-Type':'application/json'}
            for field in ('anthropic-version','anthropic-beta'):
                if self.headers.get(field):headers[field]=self.headers[field]
            connection.request('POST',self.path,self.rfile.read(size),headers)
            response=connection.getresponse();self.send_response(response.status);self.send_header('Content-Type',response.getheader('Content-Type','application/json'));self.end_headers()
            while data:=response.read(8192):self.wfile.write(data);self.wfile.flush()
        finally:connection.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        try:
            from control_store import identifier
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            task=identifier(body['task_id'])
            if self.path=='/cancel':
                with ACTIVE_LOCK:process=ACTIVE.get(task)
                if process:terminate(process)
                value={'cancelled':bool(process),'message':'已请求终止任务' if process else '任务当前未运行'}
            elif self.path=='/run':
                with BUSY:
                    process=subprocess.Popen(['/lsiopy/bin/python3','/bot/harness_task.py'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,start_new_session=True)
                    with ACTIVE_LOCK:ACTIVE[task]=process
                    try:
                        try:output,_=process.communicate(json.dumps(body,ensure_ascii=False),timeout=150)
                        except subprocess.TimeoutExpired:terminate(process);raise RuntimeError('工具任务超过 150 秒，已终止')
                        if not output.strip():raise RuntimeError('工具进程已终止，未返回完成结果')
                        value=json.loads(output)
                        if process.returncode or 'error' in value:raise RuntimeError(value.get('error','工具任务没有完成'))
                    finally:
                        with ACTIVE_LOCK:ACTIVE.pop(task,None)
            else:raise ValueError('未知工具服务接口')
            data=json.dumps(value,ensure_ascii=False).encode();self.send_response(200)
        except Exception as error:
            data=json.dumps({'error':str(error) if isinstance(error,RuntimeError) else type(error).__name__}).encode();self.send_response(400)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):daemon_threads=True


if __name__=='__main__':
    os.umask(0o077)
    proxy=ThreadingHTTPServer(('127.0.0.1',18081),Proxy);threading.Thread(target=proxy.serve_forever,daemon=True).start()
    path=Path('/harness/control.sock')
    if path.exists():path.unlink()
    server=Server(str(path),Handler);os.chmod(path,0o666);server.serve_forever()
