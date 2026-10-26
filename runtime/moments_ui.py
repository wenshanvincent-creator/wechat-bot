"""Owned Linux WeChat text-Moment composer, with explicit visibility readback."""
import fcntl
import hashlib
import re
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from Xlib import X,display
from PIL import Image
from group_sender import ROOT,restore_main
from ui_lock import Locked

TEMPLATES=Path(__file__).parent/'moments-templates'
REGIONS={'camera':(57,17,76,34),'cancel':(223,418,335,448),
         'visibility':(100,335,170,355),'private_choice':(65,215,140,250),
         'private_value':(290,335,337,355),'friends_value':(290,335,337,355),
         'discard':(224,292,336,322)}


class MomentsDesktop:
    def __init__(self):
        self.identity=json.loads((ROOT/'verified-desktop.json').read_text())
        self.connection=display.Display(':1');self.root=self.connection.screen().root
        self.window=None

    def owned(self):
        windows=[]
        result=subprocess.run(['xdotool','search','--onlyvisible','--class','^wechat$'],capture_output=True,text=True,timeout=5)
        if result.returncode not in (0,1):raise RuntimeError('无法枚举微信窗口')
        for raw in result.stdout.splitlines():
            w=self.connection.create_resource_object('window',int(raw));g=w.get_geometry();p=self.root.translate_coords(w,0,0)
            owner=w.get_full_property(self.connection.intern_atom('_NET_WM_PID'),X.AnyPropertyType)
            if owner and int(owner.value[0])==self.identity['wechat_pid'] and (p.x,p.y,g.width,g.height)==(420,120,440,560):windows.append(w)
        if len(windows)!=1:raise RuntimeError('没有唯一的机器人朋友圈窗口')
        self.window=windows[0];return self.window

    def capture(self,region):
        self.owned();x,y,x1,y1=region
        raw=self.root.get_image(420+x,120+y,x1-x,y1-y,X.ZPixmap,0xffffffff)
        return Image.frombytes('RGB',(x1-x,y1-y),raw.data,'raw','BGRX')

    def check(self,name):
        expected=Image.open(TEMPLATES/(name+'.png')).convert('RGB');actual=self.capture(REGIONS[name])
        if actual.size!=expected.size or sum(abs(a-b) for a,b in zip(actual.tobytes(),expected.tobytes()))/len(actual.tobytes())>1.5:
            raise RuntimeError('朋友圈界面与已核对的 '+name+' 控件不符')

    def wait_check(self,name):
        deadline=time.monotonic()+3
        while True:
            try:self.check(name);return
            except RuntimeError:
                if time.monotonic()>=deadline:raise
                time.sleep(.1)

    def click(self,x,y):
        self.owned();subprocess.run(['xdotool','mousemove',str(420+x),str(120+y),'click','1'],check=True,timeout=5)

    def key(self,*keys):
        self.owned();subprocess.run(['xdotool','key','--clearmodifiers',*keys],check=True,timeout=5)

    def clipboard(self,text=None):
        if text is None:return subprocess.check_output(['xclip','-selection','clipboard','-o'],text=True,timeout=5)
        subprocess.run(['xclip','-selection','clipboard'],input=text,text=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=5)

    def open(self):
        main=self.connection.create_resource_object('window',self.identity['window_id'])
        g=main.get_geometry();p=self.root.translate_coords(main,0,0)
        owner=main.get_full_property(self.connection.intern_atom('_NET_WM_PID'),X.AnyPropertyType)
        if not owner or int(owner.value[0])!=self.identity['wechat_pid'] or (p.x,p.y,g.width,g.height)!=(200,80,880,640):raise RuntimeError('微信主窗口身份或位置改变')
        try:self.owned()
        except RuntimeError:
            restore_main(self.connection,self.root,main,self.identity['wechat_pid'])
            subprocess.run(['xdotool','windowactivate','--sync',str(main.id),'mousemove','230','339','click','1'],check=True,timeout=5);time.sleep(.7)
        self.wait_check('camera')
        subprocess.run(['xdotool','mousemove','486','145','mousedown','1','sleep','1.6','mouseup','1'],check=True,timeout=5);time.sleep(.3)
        self.wait_check('cancel');self.check('visibility')

    def compose(self,text,visibility):
        self.open();self.click(130,130)
        marker='__moment_empty_'+str(time.monotonic_ns());self.clipboard(marker);self.key('ctrl+a','ctrl+c')
        time.sleep(.4)
        if self.clipboard() not in (marker,''):raise RuntimeError('朋友圈已有未处理草稿')
        self.clipboard(text);self.key('ctrl+v');time.sleep(.7);self.clipboard(marker);self.key('ctrl+a','ctrl+c');time.sleep(.4)
        if self.clipboard()!=text:raise RuntimeError('朋友圈正文回读不一致')
        self.key('Right');self.check('visibility');self.click(350,345);time.sleep(.2)
        self.wait_check('private_choice');self.click(78,230 if visibility=='private' else 167);time.sleep(.4)
        self.click(160,450);self.wait_check('cancel');self.wait_check(visibility+'_value')

    def cancel(self):
        self.check('cancel');self.click(280,433);time.sleep(.2)
        self.wait_check('discard');self.click(280,307)
        self.wait_check('camera');self.click(407,25)


def main():
    os.environ['DISPLAY']=':1';request=json.load(sys.stdin)
    if request['action'] not in ('prepare','publish'):raise ValueError('未知朋友圈操作')
    if not re.fullmatch(r'[a-z0-9-]{8,64}',request.get('operation_id','')):raise ValueError('无效操作 ID')
    selected=json.loads((ROOT/'conversations.json').read_text())
    if not selected or {x['account_ref'] for x in selected.values()}!={request['account_ref']}:raise ValueError('朋友圈账号未绑定')
    if not isinstance(request.get('text'),str) or not 1<=len(request['text'])<=2000 or request.get('visibility') not in ('private','friends'):raise ValueError('无效正文或可见范围')
    with Locked(ROOT,timeout=30):
        app=MomentsDesktop()
        try:
            app.compose(request['text'],request['visibility'])
            if request['action']=='prepare':
                app.cancel();result={'status':'prepared_not_sent','message':'正文与可见范围已回读核对，草稿测试已取消，没有发布'}
            else:
                if (ROOT.parent/'STOP').exists():raise RuntimeError('机器人已停止')
                claim=ROOT/'send-claims'/('moment-'+request['operation_id']+'.json');claim.parent.mkdir(exist_ok=True)
                with claim.open('x') as file:
                    json.dump({'text_sha256':hashlib.sha256(request['text'].encode()).hexdigest(),'visibility':request['visibility'],'at':time.time()},file);file.flush();os.fsync(file.fileno())
                app.click(160,433)
                result={'status':'submitted','message':'已点击发表，需核对朋友圈实际显示；不会自动重发','visibility':request['visibility'],'submitted_at':time.time()}
            print(json.dumps(result,ensure_ascii=False))
        finally:app.connection.close()


if __name__=='__main__':main()
