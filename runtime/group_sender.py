"""Isolated bot group sender, derived from the owned Attention GUI driver.

Only operator-enrolled groups for the enrolled bot account are accepted.
Request JSON is read from stdin; message contents are not command-line arguments.
"""
import base64
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import time
from pathlib import Path

from ui_lock import Locked

os.environ['DISPLAY'] = ':1'
from Xlib import X, display
from PIL import Image

ROOT = Path('/state/ui')
class DeliveryExpired(Exception):
    pass

spec = importlib.util.spec_from_file_location('bounded', '/bot/desktop_helpers.py')
bounded = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bounded)


def restore_main(connection,root,window,pid):
    if window.get_attributes().map_state==X.IsViewable:return
    # The owned 24px WeChat tray icon restores Qt's internal visibility state;
    # mapping the X window directly is immediately undone by the client.
    import subprocess
    icons=[]
    result=subprocess.run(['xdotool','search','--onlyvisible','--class','^wechat$'],capture_output=True,text=True,timeout=5)
    for raw in result.stdout.splitlines():
        icon=connection.create_resource_object('window',int(raw));g=icon.get_geometry();p=root.translate_coords(icon,0,0)
        owner=icon.get_full_property(connection.intern_atom('_NET_WM_PID'),X.AnyPropertyType)
        if owner and int(owner.value[0])==pid and (p.x,p.y,g.width,g.height)==(4,4,24,24):icons.append(icon)
    if len(icons)!=1:raise RuntimeError('找不到已绑定账号的微信托盘图标')
    subprocess.run(['xdotool','mousemove','--window',str(icons[0].id),'12','12','click','1'],check=True,timeout=5)
    deadline=time.monotonic()+3
    while window.get_attributes().map_state!=X.IsViewable:
        if time.monotonic()>deadline:raise RuntimeError('微信主窗口没有从托盘恢复')
        time.sleep(.1)


class Conversation(bounded.Desktop):
    def __init__(self, selected):
        self.selected = selected
        identity = json.loads((ROOT/'verified-desktop.json').read_text())
        self.pid = identity['wechat_pid']
        self.connection = display.Display()
        self.root = self.connection.screen().root
        self.window = self.connection.create_resource_object('window', identity['window_id'])
        try:
            self.check_window()
        except Exception:
            # WeChat recreates its main window on a tray round-trip, which kills
            # the stored id and every later capture with BadWindow. Find the live
            # window again and remember it instead of failing the whole action.
            self.window = self.find_main()
            self.check_window()
            (ROOT/'verified-desktop.json').write_text(
                json.dumps({**identity,'window_id':self.window.id}),encoding='utf-8')


    def find_main(self):
        import subprocess
        windows=[]
        result=subprocess.run(['xdotool','search','--onlyvisible','--class','^wechat$'],
                               capture_output=True,text=True,timeout=5)
        for raw in result.stdout.splitlines():
            window=self.connection.create_resource_object('window',int(raw))
            try:
                owner=window.get_full_property(self.connection.intern_atom('_NET_WM_PID'),X.AnyPropertyType)
                geo=window.get_geometry();pos=self.root.translate_coords(window,0,0)
            except Exception:continue
            if owner and int(owner.value[0])==self.pid and window.get_wm_class()==('wechat','wechat') and geo.width>=600 and geo.height>=400:
                windows.append(window)
        if len(windows)!=1:raise RuntimeError('微信主窗口不唯一或已关闭')
        from window_geometry import restore
        restore(windows[0],self.root,self.run)
        return windows[0]


    def check_window(self):
        try:
            geom = self.window.get_geometry()
            pos = self.root.translate_coords(self.window, 0, 0)
            owner = self.window.get_full_property(self.connection.intern_atom('_NET_WM_PID'), X.AnyPropertyType)
        except Exception:
            # The handle died after it was resolved; recover once by pid+geometry.
            self.window = self.find_main()
            geom = self.window.get_geometry()
            pos = self.root.translate_coords(self.window, 0, 0)
            owner = self.window.get_full_property(self.connection.intern_atom('_NET_WM_PID'), X.AnyPropertyType)
            identity = json.loads((ROOT/'verified-desktop.json').read_text())
            (ROOT/'verified-desktop.json').write_text(
                json.dumps({**identity,'window_id':self.window.id}),encoding='utf-8')
        if not owner or int(owner.value[0]) != self.pid or self.window.get_wm_class() != ('wechat', 'wechat'):
            raise RuntimeError('Verified WeChat process changed')
        from window_geometry import restore
        restore(self.window,self.root,self.run)


    def check_scope(self):
        self.check_window()
        x,y,width,height=self.selected['title_rect']
        if hashlib.sha256(self.capture((x,y,x+width,y+height)).tobytes()).hexdigest()!=self.selected['title_hash']:
            raise RuntimeError('Enrolled conversation title changed')

    def open_selected(self):
        self.check_window()
        restore_main(self.connection,self.root,self.window,self.pid)
        self.run('xdotool','windowactivate','--sync',str(self.window.id))
        self.run('xdotool','mousemove','--window',str(self.window.id),'163','58','click','1')
        self.run('xdotool','key','--clearmodifiers','ctrl+a','BackSpace')
        self.set_clipboard(self.selected['name'])
        self.run('xdotool','key','--clearmodifiers','ctrl+v')
        time.sleep(1.0)
        # Enter opens the top exact-name hit; this keeps working after a rename
        # even when the dropdown is hard to capture.
        self.run('xdotool','key','--clearmodifiers','Return')
        time.sleep(0.9)
        try:
            self.check_scope();return
        except Exception:
            pass
        if not self.selected.get('search_template_png'):
            raise RuntimeError('No unique enrolled search result; no conversation opened')
        template=Image.open(io.BytesIO(base64.b64decode(self.selected['search_template_png']))).convert('RGB')
        avatar=template.crop((12,5,40,31)).tobytes()
        # Fallback for clients whose search dropdown does not render: find the
        # chat by its avatar in the visible conversation list and click it.
        matches=[]
        for _ in range(6):
            pane=self.capture((75,80,215,500))
            hits=[y for y in range(pane.height-35)
                  if max(abs(a-b) for a,b in zip(pane.crop((12,y+5,40,y+31)).tobytes(),avatar))<=14]
            if len(hits)==1:
                matches=hits;break
            time.sleep(0.4)
        if len(matches)!=1:
            raise RuntimeError('No unique enrolled search result; no conversation opened')
        self.run('xdotool','mousemove','--window',str(self.window.id),'150',str(80+matches[0]+18),'click','1')
        time.sleep(0.5)
        self.check_scope()

    def ensure_empty(self):
        self.check_scope()
        self.click(550,550)
        token=f'__attention_empty_{time.monotonic_ns()}__'
        self.set_clipboard(token)
        self.key('ctrl+a','ctrl+c')
        time.sleep(0.2)
        targets=self.run('xclip','-selection','clipboard','-t','TARGETS','-o')
        if 'image/png' in targets:
            raise RuntimeError('Input contains an image draft; not replacing it')
        if self.clipboard() in (token,''):
            return
        # An interrupted send can leave its own text in the box, and then every
        # later reply in this chat is refused. Clear it and prove it is empty.
        self.key('ctrl+a','BackSpace')
        time.sleep(0.25)
        self.set_clipboard(token)
        self.key('ctrl+a','ctrl+c')
        time.sleep(0.2)
        if self.clipboard() not in (token,''):
            raise RuntimeError('Message input still contains a draft; not replacing it')

    def prepare(self, text):
        self.ensure_empty()
        self.set_clipboard(text)
        self.key('ctrl+v')
        time.sleep(0.3)
        token=f'__attention_verify_{time.monotonic_ns()}__'
        self.set_clipboard(token)
        self.key('ctrl+a','ctrl+c')
        time.sleep(0.2)
        if self.clipboard()!=text:
            raise RuntimeError('Message input readback differs from requested text')
        self.key('Right')

    def latest_bubble(self):
        # Reuse the validated detector, permitting longer outgoing text rectangles.
        self.check_scope()
        pane=self.capture(bounded.PANE)
        pixels=pane.load()
        bands=[]
        band=None
        for y in range(pane.height):
            xs=[x for x in range(pane.width)
                if 65<pixels[x,y][0]<200 and pixels[x,y][1]>180
                and pixels[x,y][2]<180 and pixels[x,y][1]-pixels[x,y][0]>30]
            if len(xs)>=20:
                band=([min(xs),y,max(xs)+1,y+1] if band is None else
                      [min(band[0],min(xs)),band[1],max(band[2],max(xs)+1),y+1])
            elif band is not None:
                if band[3]-band[1]>=16:
                    bands.append(band)
                band=None
        if band is not None and band[3]-band[1]>=16:
            bands.append(band)
        if not bands:
            return None
        bounds=bands[-1]
        if bounds[2]<490:
            return None
        return {'bounds':bounds,'hash':hashlib.sha256(pane.crop(bounds).tobytes()).hexdigest()}



def main():
    request=json.load(sys.stdin)
    action=request['action']
    registry=json.loads((ROOT/'conversations.json').read_text())
    selected=registry[request['recipient_id']]
    if action not in ('read','prepare','send'):
        raise ValueError('Unsupported action')
    if action=='read' and selected['kind']!='group':
        raise ValueError('Read is limited to enrolled groups')
    if action in ('prepare','send') and selected['kind'] not in ('group','friend'):
        raise ValueError('Sending is limited to explicitly enrolled bot groups/friends')
    if action in ('prepare','send'):
        text=request['text']
        if not isinstance(text,str) or not 1<=len(text)<=2000 or not text.strip():
            raise ValueError('Text must contain 1..2000 characters')
        operation=request['operation_id']
        if not re.fullmatch(r'[a-z0-9-]{8,64}',operation):
            raise ValueError('Invalid operation_id')
    if selected['account_ref'] != request.get('account_ref'):
        raise ValueError('Group enrollment belongs to another account')
    if (Path('/state')/'STOP').exists():
        raise ValueError('Bot is stopped')
    with Locked(ROOT,timeout=15):
        desktop=Conversation(selected)
        claimed=False
        response=None
        try:
            if action=='send':
                existing=ROOT/'send-claims'/f'{operation}.json'
                if existing.exists():
                    old=json.loads(existing.read_text())
                    if old['recipient_id']!=request['recipient_id'] or old['text_sha256']!=hashlib.sha256(text.encode()).hexdigest():
                        raise ValueError('Operation ID belongs to another send claim')
                    response=old.get('receipt',{'status':'uncertain','error':'Existing send claim; check conversation, do not resend'})
                    return
            desktop.open_selected()
            if action=='read':
                image=desktop.capture((302,32,878,500))
                output=io.BytesIO(); image.save(output,format='PNG')
                response={'status':'read','recipient_id':request['recipient_id'],
                    'observed_at':time.time(),'coverage':'visible viewport only; not full history',
                    'image_png':base64.b64encode(output.getvalue()).decode()}
                return
            before=desktop.latest_bubble()
            desktop.prepare(text)
            if action=='prepare':
                # Prepare verifies the draft, then clears exactly that draft without Return.
                desktop.key('ctrl+a','BackSpace')
                response={'status':'prepared_not_sent','recipient_id':request['recipient_id']}
                return
            desktop.check_scope()
            def check_deadline():
                if time.time()*1000>request['not_after_ms']:
                    raise DeliveryExpired('Delivery window expired before Return; not sent')
            check_deadline()
            claims=ROOT/'send-claims'; claims.mkdir(mode=0o700,exist_ok=True)
            path=claims/f'{operation}.json'
            with path.open('x',encoding='utf-8') as claim:
                json.dump({'operation_id':operation,'recipient_id':request['recipient_id'],
                           'text_sha256':hashlib.sha256(text.encode()).hexdigest(),'claimed_at':time.time()},claim)
                claim.flush(); os.fsync(claim.fileno())
            descriptor=os.open(claims,os.O_DIRECTORY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
            claimed=True
            check_deadline()
            if (Path('/state')/'STOP').exists():
                raise RuntimeError('Bot stopped before submission')
            desktop.key('Return')
            deadline=time.monotonic()+6
            while time.monotonic()<deadline:
                after=desktop.latest_bubble()
                if after and (before is None or after['hash']!=before['hash']):
                    if desktop.copy_bubble(after)==text:
                        receipt={'status':'sent','recipient_id':request['recipient_id'],'visible_in_conversation':True,
                                 'observed_at':time.time(),'bubble_hash':after['hash'],'recipient_read':None}
                        bounded.save(path,{**json.loads(path.read_text()),'receipt':receipt})
                        response=receipt; return
                    raise RuntimeError('Newest outgoing message differs from requested text')
                time.sleep(0.2)
            raise RuntimeError('No distinct matching outgoing bubble observed; do not resend')
        except DeliveryExpired as error:
            response={'status':'expired','error':str(error),'send_attempted':False}
        except Exception as error:
            response={'status':'uncertain' if claimed else 'failed',
                      'send_claimed':claimed,'error':str(error)}
        finally:
            desktop.close()
            if response is not None:
                print(json.dumps(response))


if __name__=='__main__':
    main()
