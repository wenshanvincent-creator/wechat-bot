import io
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from control_store import ControlStore
from media_memory import MediaMemory
from media_bytes import load
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from PIL import Image


def test_identical_image_is_understood_once_and_only_short_description_is_retained(tmp_path,monkeypatch):
    import sticker_catalog
    monkeypatch.setattr(sticker_catalog,'DYNAMIC',tmp_path/'media'/'stickers')
    c=ControlStore(tmp_path);memory=MediaMemory(c);calls=[]
    class Client:
        def vision(self,data):
            calls.append(data)
            return {'description':'鲸鱼疑惑地看着前方','text':'啊？','is_sticker':True,'safe_to_reuse':True,'sticker_name':'鲸鱼疑惑不解','meaning':'没听懂或很困惑','reason':'可复用的卡通表情'},{}
    image=Image.new('RGB',(20,20));buffer=io.BytesIO();image.save(buffer,format='PNG');data=buffer.getvalue()
    result,cached=memory.process_bytes(data,Client());again,cached2=memory.process_bytes(data,Client())
    assert not cached and cached2 and len(calls)==1 and result==again
    assert result['admitted'] and '图中文字' in result['description']
    item,path=sticker_catalog.resolve('鲸鱼疑惑不解');assert path.read_bytes()==data
    assert b'base64' not in (tmp_path/'control.sqlite').read_bytes()


def test_private_photo_rejection_does_not_create_a_reusable_sticker(tmp_path):
    c=ControlStore(tmp_path);memory=MediaMemory(c)
    class Client:
        def vision(self,data):return {'description':'私人合照','is_sticker':False,'safe_to_reuse':False,'reason':'私人照片'},{}
    result,_=memory.process_bytes(b'test-image-bytes',Client())
    assert not result['admitted'] and not (tmp_path/'media'/'stickers').exists()


def test_received_encrypted_emoji_decodes_without_persisting_key(tmp_path):
    key=b'0123456789abcdef';image=Image.new('RGB',(12,12),(1,2,3));buffer=io.BytesIO();image.save(buffer,format='PNG');data=buffer.getvalue()
    pad=16-len(data)%16;enc=Cipher(algorithms.AES(key),modes.CBC(key)).encryptor();encrypted=enc.update(data+bytes([pad])*pad)+enc.finalize()
    digest='a'*32;path=tmp_path/'cache'/'2026-10'/'Emoticon'/'aa'/digest;path.parent.mkdir(parents=True);path.write_bytes(encrypted)
    class Reader:
        databases=[{'path':tmp_path/'db_storage'/'message'/'message_0.db'}]
        def media_details(self,message):return {'aeskey':key.hex()}
    actual=load(Reader(),{'media':{'kind':'sticker','md5':digest}})
    assert Image.open(io.BytesIO(actual)).getpixel((0,0))==(1,2,3)
