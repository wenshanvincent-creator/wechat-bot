"""Bounded server-only filehelper trial using the existing X11 desktop.

Only exact 测试 -> 收到. No general recipient, text, or scheduling interface.
Run once per round; durable send claims and an exclusive lock prevent retries.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

os.environ['DISPLAY'] = ':1'
from Xlib import X, display
from PIL import Image

DIRECTORY = Path('/config/attention-filehelper')
TITLE_HASH = 'db21ab7bb7acfac9f2bc1631d5ddb1c5a0541a85e98d471fed1deb33ba993dc7'
PANE = (310, 85, 810, 490)

class Desktop:
    def __init__(self):
        identity = json.loads((DIRECTORY/'verified-desktop.json').read_text(encoding='utf-8'))
        self.pid = identity['wechat_pid']
        self.connection = display.Display()
        self.root = self.connection.screen().root
        self.window = self.connection.create_resource_object('window', identity['window_id'])
        self.check_scope()

    def run(self, *args):
        return subprocess.check_output(args, text=True, timeout=5)

    def capture(self, rect):
        x, y, x1, y1 = rect
        raw = self.root.get_image(200+x, 80+y, x1-x, y1-y, X.ZPixmap, 0xffffffff)
        return Image.frombytes('RGB', (x1-x, y1-y), raw.data, 'raw', 'BGRX')

    def check_scope(self):
        geom = self.window.get_geometry()
        pos = self.root.translate_coords(self.window, 0, 0)
        pid = self.window.get_full_property(self.connection.intern_atom('_NET_WM_PID'), X.AnyPropertyType)
        if not pid or int(pid.value[0]) != self.pid or self.window.get_wm_class() != ('wechat', 'wechat'):
            raise RuntimeError('Verified WeChat process changed')
        if (geom.width, geom.height, pos.x, pos.y) != (880, 640, 200, 80):
            raise RuntimeError('Verified desktop geometry changed')
        if hashlib.sha256(self.capture((310,38,520,74)).tobytes()).hexdigest() != TITLE_HASH:
            raise RuntimeError('Filehelper title changed or is obscured')

    def latest_bubble(self):
        self.check_scope()
        pane = self.capture(PANE)
        pix = pane.load()
        bands, current = [], None
        for y in range(pane.height):
            xs = []
            for x in range(pane.width):
                r,g,b = pix[x,y]
                if 65 < r < 200 and g > 180 and b < 180 and g-r > 30:
                    xs.append(x)
            if len(xs) >= 20:
                current = ([min(xs),y,max(xs)+1,y+1] if current is None else
                           [min(current[0],min(xs)),current[1],max(current[2],max(xs)+1),y+1])
            elif current is not None:
                if current[3]-current[1] >= 16:
                    bands.append(current)
                current = None
        if current is not None and current[3]-current[1] >= 16:
            bands.append(current)
        if not bands:
            return None
        bounds = bands[-1]
        x,y,x1,y1 = bounds
        # Only the observed short, right-aligned text bubble shape is supported.
        if x1 < 490 or not 35 <= x1-x <= 90 or not 25 <= y1-y <= 65:
            return None
        return {'bounds': bounds, 'hash': hashlib.sha256(pane.crop(bounds).tobytes()).hexdigest()}

    def set_clipboard(self, text):
        subprocess.run(['xclip','-selection','clipboard'], input=text, text=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=5)

    def clipboard(self):
        return self.run('xclip','-selection','clipboard','-o')

    def key(self, *keys):
        self.check_scope()
        self.run('xdotool','key','--clearmodifiers',*keys)

    def click(self, x, y, button=1):
        self.check_scope()
        self.run('xdotool','mousemove','--window',str(self.window.id),str(x),str(y),'click',str(button))

    def copy_bubble(self, bubble):
        self.check_scope()
        self.run('xdotool','windowactivate','--sync',str(self.window.id))
        token = f'__attention_copy_{time.monotonic_ns()}__'
        self.set_clipboard(token)
        x,y,x1,y1 = bubble['bounds']
        self.click(PANE[0]+(x+x1)//2, PANE[1]+(y+y1)//2, 3)
        menus = []
        for _ in range(10):
            menus = []
            for window in self.root.query_tree().children:
                if window.get_wm_class() != ('wechat','wechat'):
                    continue
                attr = window.get_attributes()
                if attr.map_state != X.IsViewable or not attr.override_redirect:
                    continue
                geo = window.get_geometry()
                pid = window.get_full_property(self.connection.intern_atom('_NET_WM_PID'), X.AnyPropertyType)
                if pid and int(pid.value[0]) == self.pid and geo.width == 162 and 330 <= geo.height <= 430:
                    menus.append(window)
            if menus:
                break
            time.sleep(0.1)
        if len(menus) != 1:
            self.key('Escape')
            raise RuntimeError('No unique verified text copy menu')
        menu = menus[0]
        position = self.root.translate_coords(menu,0,0)
        # Copy was visually verified as the first menu row for this text bubble.
        self.check_scope()
        self.run('xdotool','mousemove',str(position.x+81),str(position.y+21),'click','1')
        time.sleep(0.2)
        value = self.clipboard()
        if value == token:
            raise RuntimeError('Message copy did not update the clipboard')
        return value

    def prepare_reply(self):
        self.check_scope()
        self.click(550,550)
        token = f'__attention_empty_{time.monotonic_ns()}__'
        self.set_clipboard(token)
        self.key('ctrl+a','ctrl+c')
        time.sleep(0.2)
        if self.clipboard() not in (token, ''):
            raise RuntimeError('Input contains a draft; not replacing it')
        self.set_clipboard('收到')
        self.key('ctrl+v')
        time.sleep(0.3)
        self.set_clipboard(token)
        self.key('ctrl+a','ctrl+c')
        time.sleep(0.2)
        if self.clipboard() != '收到':
            raise RuntimeError('Reply input verification failed')
        self.key('Right')

    def verify_reply(self, incoming_hash):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            bubble = self.latest_bubble()
            if bubble and bubble['hash'] != incoming_hash:
                if self.copy_bubble(bubble) == '收到':
                    return bubble
                raise RuntimeError('Newest message after send is not the expected reply')
            time.sleep(0.2)
        raise RuntimeError('No new reply bubble observed after send; do not retry')

    def close(self):
        self.connection.close()

def save(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w',encoding='utf-8') as output:
        json.dump(value,output,ensure_ascii=True)
        output.flush()
        os.fsync(output.fileno())
    temp.replace(path)
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode',choices=['probe','run'])
    parser.add_argument('--round')
    parser.add_argument('--seconds',type=int,default=3600)
    parser.add_argument('--max-replies',type=int,default=5)
    args = parser.parse_args()
    DIRECTORY.mkdir(mode=0o700,exist_ok=True)
    with (DIRECTORY/'runtime.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        desktop = Desktop()
        try:
            baseline = desktop.latest_bubble()
            if baseline is None:
                raise RuntimeError('No supported baseline text bubble; not armed')
            baseline_text = desktop.copy_bubble(baseline)
            if baseline_text not in ('测试','收到'):
                raise RuntimeError('Baseline is not an approved test/reply text')
            if args.mode == 'probe':
                print(json.dumps({'baseline_text':baseline_text,'hash':baseline['hash'],'sending_enabled':False},ensure_ascii=True))
                return
            if not args.round or not re.fullmatch(r'[a-z0-9-]{1,64}',args.round):
                raise ValueError('A unique round name is required')
            if not 1 <= args.max_replies <= 5 or not 1 <= args.seconds <= 3900:
                raise ValueError('Trial limit: max five replies and 3900 seconds')
            state_path = DIRECTORY/f'{args.round}.json'
            state = {'round':args.round,'status':'starting','started_at':time.time(),
                     'duration_seconds':args.seconds,'max_replies':args.max_replies,
                     'received_triggers':0,'send_attempts':0,'verified_replies':0,'errors':0,
                     'baseline_text':baseline_text,'old_messages_ignored':True}
            with state_path.open('x',encoding='utf-8') as output:
                json.dump(state,output)
            with (DIRECTORY/f'{args.round}.jsonl').open('a',encoding='utf-8',buffering=1) as log:
                def emit(event,**fields):
                    log.write(json.dumps({'time':time.time(),'event':event,**fields})+'\n')
                state['status'] = 'armed'
                save(state_path,state)
                emit('armed',**state)
                previous_hash = baseline['hash']
                started = heartbeat = time.monotonic()
                try:
                    while time.monotonic()-started < args.seconds:
                        if (DIRECTORY/'stop-runtime').exists():
                            state['status'] = 'stopped_by_command'
                            break
                        desktop.check_scope()
                        if state['send_attempts'] < args.max_replies:
                            bubble = desktop.latest_bubble()
                            if bubble and bubble['hash'] != previous_hash:
                                text = desktop.copy_bubble(bubble)
                                previous_hash = bubble['hash']
                                if text == '测试':
                                    state['received_triggers'] += 1
                                    save(state_path,state)
                                    emit('trigger',index=state['received_triggers'],bubble_hash=bubble['hash'])
                                    desktop.prepare_reply()
                                    # Record before Return; a crash or uncertain send must never cause a retry.
                                    state['send_attempts'] += 1
                                    state['status'] = 'send_attempting'
                                    save(state_path,state)
                                    emit('send_attempting',index=state['send_attempts'])
                                    desktop.key('Return')
                                    result = desktop.verify_reply(bubble['hash'])
                                    previous_hash = result['hash']
                                    state['verified_replies'] += 1
                                    state['status'] = ('observing_after_limit' if state['send_attempts'] == args.max_replies else 'armed')
                                    save(state_path,state)
                                    emit('reply_visually_verified',index=state['verified_replies'],bubble_hash=previous_hash)
                        if time.monotonic()-heartbeat >= 30:
                            state['last_heartbeat'] = time.time()
                            save(state_path,state)
                            emit('heartbeat',**state)
                            heartbeat = time.monotonic()
                        time.sleep(1)
                    else:
                        state['status'] = 'observation_expired'
                except Exception as error:
                    state['errors'] += 1
                    state['status'] = 'send_uncertain' if state['send_attempts'] > state['verified_replies'] else 'stopped_on_error'
                    state['error'] = str(error)
                    emit('error',**state)
                finally:
                    state['ended_at'] = time.time()
                    state['cleanup_complete'] = True
                    save(state_path,state)
                    emit('cleanup_complete',**state)
        finally:
            desktop.close()

if __name__ == '__main__':
    main()
