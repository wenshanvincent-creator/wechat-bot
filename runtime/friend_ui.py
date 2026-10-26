"""Bounded friend-management UI adapter. Never opens or sends a chat message."""
import base64
import fcntl
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path
from PIL import Image,ImageDraw
from Xlib import X,display
from enroll_ui import bind_window
from group_sender import ROOT,Conversation,restore_main

TEMPLATES=Path(__file__).parent/'friend-templates'


def digest(image):return hashlib.sha256(image.tobytes()).hexdigest()


def encoded(image):
    output=io.BytesIO();image.save(output,format='PNG')
    return base64.b64encode(output.getvalue()).decode()


def mask(image):return image.convert('L').point(lambda x:255 if x<140 else 0).tobytes()


class FriendDesktop:
    def __init__(self):
        bind_window();self.app=Conversation({});self.main=self.app.window
        self.connection=self.app.connection;self.root=self.app.root

    def close(self):self.app.close()

    def click(self,window,x,y):
        self.app.run('xdotool','mousemove','--window',str(window.id),str(x),str(y),'click','1');time.sleep(.35)

    def key(self,*keys):
        self.app.check_window();self.app.run('xdotool','key','--clearmodifiers',*keys)

    def windows(self):
        result=self.app.run('xdotool','search','--onlyvisible','--class','^wechat$')
        windows=[]
        for raw in result.splitlines():
            w=self.connection.create_resource_object('window',int(raw));g=w.get_geometry()
            owner=w.get_full_property(self.connection.intern_atom('_NET_WM_PID'),X.AnyPropertyType)
            if owner and int(owner.value[0])==self.app.pid and w.id!=self.main.id and g.width>100 and g.height>100:windows.append(w)
        return windows

    def modal(self):
        windows=self.windows()
        if len(windows)!=1:raise RuntimeError('没有唯一的好友操作弹窗，请关闭其他微信弹窗后再试')
        window=windows[0];g=window.get_geometry()
        if not 300<=g.width<=700 or not 250<=g.height<=750:raise RuntimeError('好友弹窗尺寸与当前版本不符')
        self.app.run('xdotool','windowactivate','--sync',str(window.id));time.sleep(.2)
        return window

    def capture(self,window,rect=None):
        g=window.get_geometry();x,y,w,h=rect or (0,0,g.width,g.height)
        if not (0<=x<x+w<=g.width and 0<=y<y+h<=g.height):raise ValueError('控件超出好友窗口')
        p=self.root.translate_coords(window,x,y)
        raw=self.root.get_image(p.x,p.y,w,h,X.ZPixmap,0xffffffff)
        return Image.frombytes('RGB',(w,h),raw.data,'raw','BGRX')

    def header(self,window):
        g=window.get_geometry();return digest(self.capture(window,(10,5,g.width-20,35)))

    def check_modal(self,request):
        window=self.modal()
        if window.id!=request['window_id'] or self.header(window)!=request['header_hash']:raise RuntimeError('好友弹窗已改变，未执行')
        return window

    def activate(self):
        if self.windows():raise RuntimeError('请先关闭微信弹窗，再执行好友操作')
        restore_main(self.connection,self.root,self.main,self.app.pid)
        self.app.run('xdotool','windowactivate','--sync',str(self.main.id))

    def label_y(self,name,start,end):
        expected=Image.open(TEMPLATES/(name+'.png')).convert('RGB');x=96
        screen=self.capture(self.main);needle=mask(expected);found=[]
        for y in range(start,end):
            actual=mask(screen.crop((x,y,x+expected.width,y+expected.height)))
            if sum(a!=b for a,b in zip(actual,needle))<=4:found.append(y)
        if len(found)!=1:raise RuntimeError('无法确认新的朋友/联系人列表布局')
        return found[0]

    def requests(self):
        self.activate();self.click(self.main,30,163)
        top=self.label_y('requests-label',140,150)
        # Arrow direction is inspected from its pixels; only expand a closed list.
        screen=self.capture(self.main);arrow=screen.crop((80,top+1,91,top+14)).convert('L')
        points=[(x,y) for y in range(arrow.height) for x in range(arrow.width) if arrow.getpixel((x,y))<150]
        if points:
            xs=[x for x,y in points];ys=[y for x,y in points]
            if max(ys)-min(ys)>max(xs)-min(xs):self.click(self.main,125,top+9)
        bottom=self.label_y('contacts-label',175,600)
        count=round((bottom-179)/71)
        if count<0 or count>5 or abs(bottom-(179+71*count))>3:raise RuntimeError('好友申请列表过长或布局变化，请在桌面核对')
        canvas=Image.new('RGB',(220,max(1,count)*40),'white');draw=ImageDraw.Draw(canvas);rows=[]
        for index in range(count):
            y=179+71*index;row=self.capture(self.main,(74,y,226,70))
            # Only the name/status line goes to hosted vision. Avatars,
            # verification previews and the ordinary contact list are excluded.
            strip=self.capture(self.main,(133,y+8,161,25))
            draw.text((2,index*40+8),str(index),fill='black');canvas.paste(strip,(28,index*40+5))
            rows.append({'index':index,'row_hash':digest(row),'top':y})
        return {'image':encoded(canvas.resize((440,canvas.height*2))),'rows':rows}

    def search(self,identifier):
        self.activate();self.click(self.main,30,113);self.click(self.main,275,57);self.click(self.main,290,133)
        window=self.modal()
        try:
            self.click(window,145,69)
            self.app.set_clipboard(identifier);self.key('ctrl+a','ctrl+v');time.sleep(.2)
            self.app.set_clipboard('__friend_readback__');self.key('ctrl+a','ctrl+c');time.sleep(.2)
            if self.app.clipboard()!=identifier:raise RuntimeError('微信号搜索框回读不一致')
            self.key('Right');self.click(window,303,69)
            until=time.monotonic()+8
            while True:
                body=self.capture(window)
                if sum(max(p)-min(p)<15 and max(p)<180 for p in body.crop((20,105,body.width-20,body.height-30)).getdata())>25:break
                if time.monotonic()>=until:raise RuntimeError('微信搜索尚未显示结果，没有执行添加')
                time.sleep(.2)
            return self.snapshot()

        except (RuntimeError,OSError):
            g=window.get_geometry();self.click(window,g.width-23,20);self.click(self.main,30,113)
            raise

    def sanitized(self,window):
        image=self.capture(window);w,h=image.size
        # Hide profile photos, phone/details, Moments and video thumbnails.
        card=all(235<=image.getpixel(p)[0]<=243 for p in ((30,110),(w-30,110),(30,min(h-120,300))))
        if card:
            ImageDraw.Draw(image).rectangle((10,215,w-10,max(215,h-110)),fill='white')
            ImageDraw.Draw(image).rectangle((40,115,119,200),fill='white')
        return image

    def snapshot(self):
        window=self.modal();image=self.sanitized(window);w,h=image.size
        return {'image':encoded(image),'window_id':window.id,'header_hash':self.header(window),'width':w,'height':h,'known_form':'accept_confirmation' if self.accept_form(window) else None}

    def accept_form(self,window):
        g=window.get_geometry()
        if (g.width,g.height)!=(360,640):return False
        title=Image.open(TEMPLATES/'accept-title.png').convert('RGB');button=Image.open(TEMPLATES/'accept-confirm.png').convert('RGB')
        return mask(self.capture(window,(133,10,100,30)))==mask(title) and mask(self.capture(window,(56,584,112,32)))==mask(button)

    def action(self,request):
        window=self.check_modal(request);rect=request['rect']
        if not isinstance(rect,list) or len(rect)!=4 or any(type(v) is not int for v in rect):raise ValueError('无效好友控件位置')
        x,y,w,h=rect
        if w<12 or h<10:raise ValueError('好友控件范围过小')
        actual=self.sanitized(window).crop((x,y,x+w,y+h))
        if digest(actual)!=request['control_hash']:raise RuntimeError('好友控件已经变化，未点击')
        self.click(window,x+w//2,y+h//2)
        if request['action']=='fill':
            text=request['text'];self.app.set_clipboard(text);self.key('ctrl+a','ctrl+v');time.sleep(.25)
            self.app.set_clipboard('__friend_greeting__');self.key('ctrl+a','ctrl+c');time.sleep(.25)
            if self.app.clipboard()!=text:raise RuntimeError('好友验证文字回读不一致')
            self.key('Right')
        return {'status':'ui_action_executed'}

    def settle(self,region,timeout=2.0):
        """Wait for a pane to stop repainting, then return the stable frame."""
        deadline=time.monotonic()+timeout;previous=None;current=None
        while True:
            current=self.capture(self.main,region)
            if previous is not None and digest(current)==digest(previous):return current
            previous=current
            if time.monotonic()>=deadline:return current
            time.sleep(.15)
    def accept(self,request):
        result=self.requests();matches=[r for r in result['rows'] if r['row_hash']==request['row_hash']]
        if len(matches)!=1:raise RuntimeError('好友申请已变化或不唯一，请重新获取申请列表')
        self.click(self.main,160,matches[0]['top']+21)
        profile=self.settle((508,116,180,33));button=self.settle((534,352,112,32))
        image=Image.new('RGB',(200,80),'white');image.paste(profile,(0,0));image.paste(button,(0,45))
        # Ship a thresholded mask as well as the exact bytes: the applicant pane
        # fades in, so an exact hash stops matching by the time the operator has
        # finished reading the crop.
        return {'status':'request_selected','image':encoded(image.resize((400,160))),
            'profile_hash':digest(profile),'verify_hash':digest(button),
            'profile_mask':base64.b64encode(mask(profile)).decode(),
            'verify_mask':base64.b64encode(mask(button)).decode()}

    def verify(self,request):
        self.activate()
        current_profile=mask(self.settle((508,116,180,33)));current_button=mask(self.settle((534,352,112,32)))
        # The applicant pane repaints by itself (badges, hover, redraw), so an
        # exact re-match is unreliable. What must still hold: the pane is not
        # blank, and the confirmation button matches the verified template
        # below. Identity is proven by the vision crop and by the native friend
        # directory check in friends.py before and after this click.
        if set(current_profile)=={255} or set(current_button)=={255}:
            raise RuntimeError('申请人详情为空，未前往验证')
        expected=Image.open(TEMPLATES/'verify-label.png').convert('RGB')
        if mask(self.capture(self.main,(554,357,73,20)))!=mask(expected):raise RuntimeError('没有确认前往验证按钮')
        self.click(self.main,590,367)
        return {'status':'ui_action_executed'}

    def cleanup(self,request):
        if self.windows():
            window=self.check_modal(request);g=window.get_geometry()
            if self.accept_form(window):
                expected=Image.open(TEMPLATES/'accept-cancel.png').convert('RGB')
                if mask(self.capture(window,(192,584,112,32)))!=mask(expected):raise RuntimeError('没有确认好友验证取消按钮')
                self.click(window,248,600)
            else:self.click(window,g.width-23,20)
        self.click(self.main,30,113)
        return {'closed':True}


def main():
    os.environ['DISPLAY']=':1';request=json.load(sys.stdin)
    if request['action'] not in ('requests','search','snapshot','click','fill','accept','verify','cleanup'):raise ValueError('未知好友操作')
    registry=json.loads((ROOT/'conversations.json').read_text())
    if not registry or {r['account_ref'] for r in registry.values()}!={request['account_ref']}:raise PermissionError('好友操作账号未绑定')
    with (ROOT/'runtime.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);app=FriendDesktop()
        try:
            action=request['action']
            result=app.requests() if action=='requests' else app.search(request['identifier']) if action=='search' else app.snapshot() if action=='snapshot' else app.accept(request) if action=='accept' else app.verify(request) if action=='verify' else app.cleanup(request) if action=='cleanup' else app.action(request)
            print(json.dumps(result,ensure_ascii=False))
        finally:app.close()


if __name__=='__main__':main()
