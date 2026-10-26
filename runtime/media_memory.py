"""One hosted vision pass per content hash, then compact text and model-reviewed stickers."""
import hashlib
import json
import time
from pathlib import Path
from media_bytes import load,normalize
from provider_client import ProviderClient


class MediaMemory:
    def __init__(self,control):
        self.control=control;self.db=control.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS media_assets(hash TEXT PRIMARY KEY,body TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS media_map(account TEXT,kind TEXT,md5 TEXT,hash TEXT,PRIMARY KEY(account,kind,md5));
            CREATE TABLE IF NOT EXISTS media_events(id TEXT PRIMARY KEY,channel_id TEXT,body TEXT,state TEXT,result TEXT,updated REAL);
        ''')

    def list(self):
        return [dict(id=r['id'],channel_id=r['channel_id'],state=r['state'],result=json.loads(r['result']) if r['result'] else None,updated=r['updated']) for r in self.db.execute('SELECT * FROM media_events ORDER BY updated DESC LIMIT 100')]

    def process_bytes(self,data,client,sticker_bytes=None):
        digest=hashlib.sha256(data).hexdigest();old=self.db.execute('SELECT body FROM media_assets WHERE hash=?',(digest,)).fetchone()
        if old:return json.loads(old[0]),True
        review,usage=client.vision(data)
        description=review.get('description');text=review.get('text','')
        if not isinstance(description,str) or not description.strip():raise ValueError('识图没有返回内容描述')
        brief=description[:250]+(('；图中文字：'+text[:250]) if isinstance(text,str) and text else '')
        admitted=review.get('is_sticker') is True and review.get('safe_to_reuse') is True
        sticker_bytes=sticker_bytes if sticker_bytes is not None else data
        sticker_hash=hashlib.sha256(sticker_bytes).hexdigest()
        name=review.get('sticker_name','');meaning=review.get('meaning','')
        if admitted and (not isinstance(name,str) or not 1<=len(name)<=32 or not isinstance(meaning,str) or not meaning.strip()):raise ValueError('表情审核缺少准确名称或含义')
        if admitted:
            from sticker_catalog import catalog,resolve
            known=catalog();base=name;number=2
            existing=next((s for s in known if s['sha256']==sticker_hash),None)
            if existing is None:
                for candidate in known:
                    _,asset=resolve(candidate['name'])
                    if hashlib.sha256(normalize(asset.read_bytes(),first_frame=True)).hexdigest()==sticker_hash:existing=candidate;break
            if existing:name=existing['name'];sticker_hash=existing['sha256']
            else:
                while any(s['name']==name for s in known):name=base+'（'+str(number)+'）';number+=1
                folder=self.control.root/'media';folder.mkdir(mode=0o711,exist_ok=True)
                folder.chmod(0o711);folder=folder/'stickers';folder.mkdir(mode=0o755,exist_ok=True);folder.chmod(0o755)
                file=folder/(sticker_hash+'.png');file.write_bytes(sticker_bytes);file.chmod(0o644)
                path=folder/'catalog.json';entries=json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
                entries.append({'name':name,'meaning':meaning[:200],'description':description[:250],'file':file.name,'sha256':sticker_hash,'source':'model-reviewed incoming image'})
                temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(entries,ensure_ascii=False),encoding='utf-8');temporary.chmod(0o644);temporary.replace(path)
        result={'description':brief,'admitted':admitted,'sticker_name':name if admitted else None,'reason':str(review.get('reason',''))[:250],'hash':digest,'sticker_hash':sticker_hash if admitted else None,'usage':usage}
        with self.db:self.db.execute('INSERT INTO media_assets VALUES (?,?,?)',(digest,json.dumps(result,ensure_ascii=False),time.time()))
        return result,False

    def resolve(self,reader,channel,message,client=None):
        media=message.get('media')
        if not media:return message
        event=message['message_id'];account=reader.account_ref
        with self.db:self.db.execute('INSERT OR IGNORE INTO media_events VALUES (?,?,?,?,?,?)',(event,channel['id'],json.dumps(message,ensure_ascii=False),'queued',None,time.time()))
        old=self.db.execute('SELECT a.body FROM media_map m JOIN media_assets a ON a.hash=m.hash WHERE m.account=? AND m.kind=? AND m.md5=?',(account,media['kind'],media['md5'])).fetchone()
        try:
            if old:result,cached=json.loads(old[0]),True
            else:
                provider=self.control.get('providers',self.control.meta('settings',{}).get('vision_provider_id','deepseek'))
                client=client or ProviderClient(provider,self.control.secret(provider['id']))
                source=load(reader,message,raw=True)
                result,cached=self.process_bytes(normalize(source),client,sticker_bytes=normalize(source,first_frame=True))
                with self.db:self.db.execute('INSERT OR REPLACE INTO media_map VALUES (?,?,?,?)',(account,media['kind'],media['md5'],result['hash']))
            with self.db:self.db.execute("UPDATE media_events SET state='done',result=?,updated=? WHERE id=?",(json.dumps({**result,'cached':cached},ensure_ascii=False),time.time(),event))
            return {**message,'text':('[表情] ' if media['kind']=='sticker' else '[图片] ')+result['description']}
        except Exception as error:
            reason=str(error) if isinstance(error,(ValueError,RuntimeError,OSError)) else type(error).__name__
            with self.db:self.db.execute("UPDATE media_events SET state='failed',result=?,updated=? WHERE id=?",(json.dumps({'error':reason},ensure_ascii=False),time.time(),event))
            return {**message,'text':'[收到图片，但尚未成功识别：'+reason+'。不要猜测画面内容。]'}

    def retry(self,event):
        row=self.db.execute('SELECT * FROM media_events WHERE id=?',(event,)).fetchone()
        if not row:raise ValueError('图片事件不存在')
        channel=self.control.get('channels',row['channel_id'])
        if not channel.get('enabled') or not channel.get('allow_read'):raise PermissionError('此聊天没有读取权限')
        from native_reader import NativeReader
        reader=NativeReader(private_ids=(channel['native_id'],) if channel['kind']=='friend' else ())
        self.resolve(reader,channel,json.loads(row['body']))
        result=self.db.execute('SELECT state,result FROM media_events WHERE id=?',(event,)).fetchone()
        return {'state':result['state'],**json.loads(result['result'])}
