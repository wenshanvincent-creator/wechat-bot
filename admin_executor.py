"""Operator-side host executor. It never executes queued or unapproved requests."""
import argparse
import json
import os
import platform
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def execute(command,cwd,timeout=120):
    shell=['powershell.exe','-NoProfile','-NonInteractive','-Command'] if os.name=='nt' else ['/bin/bash','-lc']
    if os.name=='nt':command='[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new();\n'+command
    try:
        process=subprocess.run(shell+[command],cwd=cwd,capture_output=True,encoding='utf-8',errors='replace',timeout=timeout)
        return {'returncode':process.returncode,'stdout':process.stdout[-16000:],'stderr':process.stderr[-16000:]}
    except subprocess.TimeoutExpired:return {'returncode':124,'stdout':'','stderr':'管理员命令超过 120 秒，已终止主进程；请核对其子进程与已发生的改动'}


class Client:
    def __init__(self,base,password):self.base=base.rstrip('/');self.password=password;self.cookie='';self.csrf=''
    def post(self,route,body):
        request=urllib.request.Request(self.base+'/api/'+route,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Cookie':self.cookie,'X-CSRF-Token':self.csrf})
        with urllib.request.urlopen(request,timeout=15) as response:
            value=json.load(response)
            if route=='login':self.cookie=response.headers['Set-Cookie'].split(';')[0];self.csrf=value['csrf']
            return value
    def login(self):self.post('login',{'password':self.password})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--url',default='http://127.0.0.1:13332');parser.add_argument('--secrets',default='private/secrets.env');parser.add_argument('--once',action='store_true')
    args=parser.parse_args();values=dict(line.split('=',1) for line in Path(args.secrets).read_text(encoding='utf-8').splitlines() if '=' in line)
    client=Client(args.url,values['CONSOLE_PASSWORD']);executor=platform.node()+'-'+platform.system();client.login()
    receipts=Path(__file__).resolve().parent/'private'/'admin-receipts';receipts.mkdir(parents=True,exist_ok=True,mode=0o700)
    def save(path,value):
        temporary=path.with_suffix('.tmp')
        with temporary.open('w',encoding='utf-8') as file:json.dump(value,file,ensure_ascii=False);file.flush();os.fsync(file.fileno())
        temporary.replace(path)
    # A crash between host execution and receipt persistence has an unknown
    # outcome. Report it for review; never execute that claim a second time.
    for path in receipts.glob('*.json'):
        record=json.loads(path.read_text(encoding='utf-8'))
        if 'result' not in record:record['result']={'returncode':125,'stdout':'','stderr':'执行器重启，未重试之前已领取的命令'};save(path,record)
    while True:
        try:
            client.post('admin/heartbeat',{'executor_id':executor})
            for path in receipts.glob('*.json'):
                record=json.loads(path.read_text(encoding='utf-8'))
                if not record.get('acknowledged'):
                    client.post('admin/finish',record);record['acknowledged']=True;save(path,record)
            result=client.post('admin/claim',{'executor_id':executor});job=result['job']
            if job:
                path=receipts/(job['id']+'.json');record={'job_id':job['id'],'claim':job['claim'],'executor_id':executor};save(path,record)
                receipt=execute(job['body']['command'],Path(__file__).resolve().parent)
                record['result']=receipt;save(path,record);client.post('admin/finish',record);record['acknowledged']=True;save(path,record)
                print(json.dumps({'job_id':job['id'],'returncode':receipt['returncode']}),flush=True)
            elif args.once:print('No approved host command; nothing executed')
        except urllib.error.HTTPError as error:
            if error.code==401:client.login()
            else:raise
        except (urllib.error.URLError,TimeoutError,ConnectionError):
            if args.once:raise
        if args.once:return
        time.sleep(5)


if __name__=='__main__':main()
