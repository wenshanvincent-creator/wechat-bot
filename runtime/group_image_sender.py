"""Owned group UI image submission with exact draft/outgoing pixel readback."""
import fcntl
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image
from group_sender import Conversation, ROOT, bounded, restore_main
from sticker_catalog import resolve


class ImageConversation(Conversation):
    def clipboard_image(self):
        data=subprocess.check_output(['xclip','-selection','clipboard','-t','image/png','-o'],timeout=5)
        return Image.open(io.BytesIO(data)).convert('RGBA')

    def prepare_image(self, path):
        self.ensure_empty()
        subprocess.run(['xclip','-selection','clipboard','-t','image/png'],input=path.read_bytes(),
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=5)
        self.key('ctrl+v');time.sleep(.3)
        self.key('ctrl+a','ctrl+c');time.sleep(.2)
        actual=self.clipboard_image();expected=Image.open(path).convert('RGBA')
        if actual.size!=expected.size or actual.tobytes()!=expected.tobytes():
            raise ValueError('Image draft pixels differ from the enrolled sticker')
        self.key('Right')

    def find_image(self,path):
        # The owned Linux client displays images at 160px width (portrait
        # images keep their taller aspect ratio),
        # right-aligned immediately left of the self avatar. Clipboard readback
        # below provides the final exact check, not this approximate locator.
        source=Image.open(path).convert('RGB')
        pane=self.capture((600,85,810,490)).convert('RGB');pixels=pane.load()
        matches=[]
        for maximum in (158,160,156):
            thumb=source.resize((maximum,round(source.height*maximum/source.width)))
            w,h=thumb.size
            samples=[(int(w*f),int(h*g)) for f in (.2,.4,.6,.8) for g in (.2,.4,.6,.8)]
            expected=[thumb.getpixel(point) for point in samples]
            for x in range(208-w-2,208-w+3):
                for y in range(pane.height-h+1):
                    error=sum(sum(abs(a-b) for a,b in zip(pixels[x+dx,y+dy],rgb))
                              for (dx,dy),rgb in zip(samples,expected))/(3*len(samples))
                    if error<8:
                        matches.append((y,error,x,w,h))
        if not matches:
            return None
        y,_,x,w,h=min(matches,key=lambda m:(m[1],-m[0]))
        return {'bounds':[600+x-310,y,600+x+w-310,y+h],'hash':'image'}

    def verify_outgoing(self,path):
        self.check_scope()
        bubble=self.find_image(path)
        if not bubble:
            return False  # Incoming messages may already have scrolled it out.
        try:
            self.copy_bubble(bubble)
            actual=self.clipboard_image();expected=Image.open(path).convert('RGBA')
            return actual.size==expected.size and actual.tobytes()==expected.tobytes()
        except RuntimeError:
            self.run('xdotool','key','Escape')
            return False


def main():
    request=json.load(sys.stdin);item,path=resolve(request['sticker'])
    selected=json.loads((ROOT/'conversations.json').read_text())[request['recipient_id']]
    if selected['kind'] not in ('group','friend') or selected['account_ref']!=request['account_ref']:
        raise ValueError('Image recipient is outside the enrolled bot group/account')
    operation=request['operation_id']
    import re
    if not re.fullmatch(r'[a-z0-9-]{8,64}',operation):
        raise ValueError('Invalid image operation ID')
    with (ROOT/'runtime.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        claim=ROOT/'send-claims'/f'{operation}.json'
        if claim.exists():
            old=json.loads(claim.read_text())
            if old['recipient_id']!=request['recipient_id'] or old.get('image_sha256')!=item['sha256']:
                raise ValueError('Image operation ID belongs to another payload')
            print(json.dumps(old.get('receipt',{'status':'uncertain'})));return
        app=ImageConversation(selected);claimed=False;draft_verified=False;submitted=False
        try:
            if Path('/state/STOP').exists():
                raise RuntimeError('Bot is stopped')
            app.open_selected();app.prepare_image(path);app.check_scope()
            draft_verified=True
            if time.time()*1000>request['not_after_ms']:
                raise RuntimeError('Image delivery expired before submission')
            claim.parent.mkdir(exist_ok=True)
            with claim.open('x') as output:
                json.dump({'recipient_id':request['recipient_id'],'image_sha256':item['sha256'],
                           'claimed_at':time.time()},output);output.flush();os.fsync(output.fileno())
            fd=os.open(claim.parent,os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
            claimed=True
            if Path('/state/STOP').exists():
                raise RuntimeError('Bot stopped before image submission')
            app.key('Return')
            submitted=True
            time.sleep(.4)
            matched=app.verify_outgoing(path)
            receipt={'status':'submitted','image_draft_matches':True,'return_pressed':True,
                     'image_readback_matches':matched,'image_sha256':item['sha256']}
            bounded.save(claim,{**json.loads(claim.read_text()),'receipt':receipt})
        except Exception as error:
            receipt={'status':'submitted' if submitted else 'uncertain' if claimed else 'failed',
                     'image_draft_matches':draft_verified,'return_pressed':submitted,
                     'error_type':type(error).__name__,'error':str(error)}
        finally:
            try:restore_main(app.connection,app.root,app.window,app.pid)
            except Exception:pass
            app.close()
        print(json.dumps(receipt))


if __name__=='__main__':
    main()
