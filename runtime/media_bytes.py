"""Read only the selected message's media cache. Decryption keys stay in RAM."""
import hashlib
import io
import re
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from PIL import Image
from public_web import fetch

def image_header(data):return data.startswith((b'\xff\xd8\xff',b'\x89PNG',b'GIF8',b'RIFF',b'wxgf'))


def decrypt(data,key,mode):
    engine=Cipher(algorithms.AES(key),mode).decryptor();return engine.update(data)+engine.finalize()


def unpad(data):
    n=data[-1] if data else 0
    return data[:-n] if 1<=n<=16 and data[-n:]==bytes([n])*n else data


def normalize(data,first_frame=False):
    # No model is run here: only decode/downsize a received image for hosted vision.
    with Image.open(io.BytesIO(data)) as source:
        if source.width*source.height>16000000:raise ValueError('图片分辨率过大，请先缩小')
        frames=[];count=getattr(source,'n_frames',1)
        for index in ([0] if first_frame else sorted({0,count//2,count-1})):
            source.seek(index);frame=source.convert('RGB');frame.thumbnail((1280,1280) if count==1 else (640,640));frames.append(frame)
        canvas=Image.new('RGB',(sum(f.width for f in frames),max(f.height for f in frames)),(255,255,255));x=0
        for frame in frames:canvas.paste(frame,(x,0));x+=frame.width
        output=io.BytesIO();canvas.save(output,format='PNG');return output.getvalue()


def load(reader,message,raw=False):
    def finish(data):return data if raw else normalize(data)
    details=reader.media_details(message);media=message['media'];root=reader.databases[0]['path'].parent.parent.parent
    if media['kind']=='sticker':
        paths=list(root.glob('cache/*/Emoticon/'+media['md5'][:2]+'/'+media['md5']))
        key=details.get('aeskey','')
        if re.fullmatch('[a-fA-F0-9]{32}',key):
            for path in paths:
                data=path.read_bytes()
                if len(data)%16:continue
                for mode in (modes.CBC(bytes.fromhex(key)),modes.CBC(bytes(16)),modes.ECB()):
                    decoded=unpad(decrypt(data,bytes.fromhex(key),mode))
                    if image_header(decoded):return finish(decoded)
        for field in ('cdnurl','thumburl'):
            url=details.get(field,'')
            if url.startswith(('https://','http://')):
                try:return finish(fetch(url,8*1024*1024,image=True))
                except (OSError,ValueError):continue
        raise ValueError('表情图片未能从本地缓存或公开媒体地址读取')
    # The client materializes exact original bytes when copying an image.
    # Match native MD5 before decoding or sending anything to hosted vision.
    temporary=root/'temp'/'RWTemp'
    for path in sorted(temporary.rglob('*'),key=lambda p:p.stat().st_mtime,reverse=True)[:200] if temporary.exists() else []:
        if path.is_file() and path.stat().st_size<=8*1024*1024:
            data=path.read_bytes()
            if hashlib.md5(data).hexdigest()==media['md5']:return finish(data)
    from bot import run_ui
    receipt=run_ui('/bot/media_ui.py',{'recipient_id':message['conversation_id'],'account_ref':reader.account_ref,'md5':media['md5']})
    path=Path(receipt['path'])
    # Re-map against the read-only profile mount, not an arbitrary returned path.
    from native_keys import PROFILE
    local=PROFILE/path.relative_to('/config')
    if not local.resolve().is_relative_to(root.resolve()):raise ValueError('图片来自其他账号')
    data=local.read_bytes()
    if hashlib.md5(data).hexdigest()!=media['md5']:raise ValueError('图片内容与原生消息不一致')
    return finish(data)
