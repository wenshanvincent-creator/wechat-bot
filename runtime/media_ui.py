"""Copy a visible image only when its native-content MD5 matches the requested event."""
import fcntl
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote,urlsplit
from group_image_sender import ImageConversation
from group_sender import ROOT,bounded
from ui_lock import Locked
from Xlib import X
from PIL import Image


def copy_at(app,x,y):
    def menus():
        values=[]
        for w in app.root.query_tree().children:
            if w.get_wm_class()!=('wechat','wechat'):continue
            a=w.get_attributes();owner=w.get_full_property(app.connection.intern_atom('_NET_WM_PID'),X.AnyPropertyType)
            if a.map_state==X.IsViewable and a.override_redirect and owner and int(owner.value[0])==app.pid:values.append(w)
        return values
    before={w.id for w in menus()};app.click(bounded.PANE[0]+x,bounded.PANE[1]+y,3)
    deadline=time.monotonic()+.8;new=[]
    while time.monotonic()<deadline:
        new=[w for w in menus() if w.id not in before]
        if new:break
        time.sleep(.1)
    if len(new)!=1:return False
    menu=new[0];position=app.root.translate_coords(menu,0,0);geo=menu.get_geometry()
    raw=app.root.get_image(position.x+25,position.y+8,60,26,X.ZPixmap,0xffffffff)
    actual=Image.frombytes('RGB',(60,26),raw.data,'raw','BGRX')
    expected=Image.open(Path(__file__).parent/'ui-templates'/'copy-label.png').convert('RGB')
    error=sum(abs(a-b) for a,b in zip(actual.tobytes(),expected.tobytes()))/len(actual.tobytes())
    if geo.width!=162 or error>1.5:
        app.key('Escape');return False # A confirmed new popup, never a bare Escape.
    app.check_scope();app.run('xdotool','mousemove',str(position.x+81),str(position.y+21),'click','1');time.sleep(.3)
    return True


def candidates(app):
    pane=app.capture(bounded.PANE);points=[]
    # Text bubbles and nicknames have few RGB colors; these tiles locate image
    # texture. Final native MD5 verification owns acceptance, not this locator.
    for x in (82,420):
        for y in range(10,pane.height-20,20):
            colors=set(pane.crop((x-10,y,x+10,y+20)).getdata())
            if len(colors)>70:points.append((x,y+10))
    return sorted(points,key=lambda p:p[1],reverse=True)


def main():
    request=json.load(sys.stdin);selected=json.loads((ROOT/'conversations.json').read_text())[request['recipient_id']]
    if selected['account_ref']!=request['account_ref']:raise ValueError('图片接收账号不匹配')
    with Locked(ROOT,timeout=30):
        app=ImageConversation(selected)
        try:
            app.open_selected();app.click(650,300,5)
            for _ in range(12):app.click(650,300,5)
            time.sleep(.3)
            attempts=0
            for page in range(5):
                for x,y in candidates(app)[:12]:
                    if attempts>=12:raise ValueError('此图片未在可见的最近消息中找到，请在控制台打开该图片后重试')
                    attempts+=1
                    try:
                        if not copy_at(app,x,y):continue
                        uri=subprocess.check_output(['xclip','-selection','clipboard','-t','text/uri-list','-o'],text=True,stderr=subprocess.DEVNULL,timeout=3).strip()
                        parts=urlsplit(uri)
                        if parts.scheme!='file' or parts.netloc:continue
                        path=Path(unquote(parts.path)).resolve()
                        if not path.is_relative_to(Path('/config/xwechat_files')) or '/temp/RWTemp/' not in str(path) or not path.is_file() or path.stat().st_size>8*1024*1024:continue
                        if hashlib.md5(path.read_bytes()).hexdigest()==request['md5']:
                            print(json.dumps({'status':'matched','path':str(path)}));return
                    except (RuntimeError,subprocess.CalledProcessError):
                        # copy_bubble already dismisses an unmatched menu. An
                        # extra Escape here hides the whole WeChat main window.
                        pass
                if page<4:
                    for _ in range(4):app.click(650,300,4)
                    time.sleep(.2)
            raise ValueError('可见图片中没有找到此消息的准确内容，不猜测配对')
        finally:app.close()


if __name__=='__main__':main()
