"""Live v2 runner: independent native intake, serial model/UI work and admin jobs."""
import hashlib
import json
import os
import queue
import signal
import threading
import time
import concurrent.futures
from datetime import datetime,timezone
from pathlib import Path

from agent_kernel import AgentKernel
import classroom
import timetable
import outbox
from bot import run_ui,send
from consumer_state import ConsumerState
from control_store import ControlStore,active_persona
from conversation_context import TokenCounter
from model_view import GroupView
from moments import Moments
from native_reader import NativeReader
from native_keys import active_account_roots
from proactive import schedule
from agent_tools import AgentTools
from harness_broker import start as start_broker,issue,revoke,call
from media_memory import MediaMemory
import moments_schedule

ROOT=Path('/state');STOP=threading.Event();INBOX=queue.Queue(maxsize=100);ACK=queue.Queue();MEDIA=queue.Queue(maxsize=40)


def directory(control,reader,self_id):
    if not self_id:self_id=active_account_roots(reader.identity)[0].parent.name.rsplit('_',1)[0]
    groups=reader.list_groups()['groups']
    entries=[{'native_id':g['conversation_id'],'name':g['remark'] or g['name'],'kind':'group'} for g in groups if not g['native_delete_flag']]
    entries.extend({'native_id':f['conversation_id'],'name':f['remark'] or f['name'],'kind':'friend'} for f in reader.list_friends() if f['conversation_id']!=self_id)
    control.set_meta('directory',entries)
    for channel in control.list('channels'):
        match=next((e for e in entries if e['native_id']==channel['native_id']),None)
        if match and channel['name']=='本地测试群':control.put('channels',{**channel,'name':match['name']})
    return entries


def collect():
    control=ControlStore(ROOT);progress=ConsumerState(ROOT/'consumer-v2.sqlite');reader=None;inflight=set();last_directory=0
    while not STOP.is_set():
        try:
            account=control.meta('account',{});self_id=account.get('self_sender_id','')
            channels=[c for c in control.list('channels') if c.get('enabled') and c.get('allow_read')]
            private={c['native_id'] for c in channels if c['kind']=='friend'}
            if reader is None or reader.private_ids!=private:reader=NativeReader(private_ids=private)
            if not account.get('account_ref'):
                directory(control,reader,'');control.set_meta('runtime',{'status':'attention','message':'微信已登录，请先在控制台绑定当前账号','heartbeat':time.time()});STOP.wait(3);continue
            if reader.account_ref!=account['account_ref']:raise RuntimeError('登录账号与绑定账号不同')
            while not ACK.empty():
                channel,batch=ACK.get_nowait();progress.acknowledge(reader,channel,'v2',batch);inflight.discard(channel)
            if time.time()-last_directory>300:
                directory(control,reader,self_id);last_directory=time.time()
            for channel in channels:
                native=channel['native_id']
                if native in inflight:continue
                progress.start(reader,native,'v2','from_now')
                batch=progress.poll(reader,native,'v2',limit=30)
                if not batch['batch_id']:continue
                view=GroupView.from_reader(reader,native,self_id,control.meta('settings',{}).get('timezone','Asia/Shanghai'))
                # Self echoes never reset the human budget or duplicate internal agent turns.
                messages=[m for m in batch['messages'] if m.get('sender_id') and m['sender_id']!=self_id]
                # Image work gets its own thread: waiting for a model turn in
                # another chat would let the picture scroll out of reach.
                queue_for_media = any(m.get('media') for m in messages)
                target = MEDIA if queue_for_media else INBOX
                target.put((channel,messages,view,batch['batch_id']),timeout=2);inflight.add(native)
            control.set_meta('runtime',{'status':'ready','message':'微信本地缓存可读；自动发送由总开关控制','heartbeat':time.time(),'account_ref':reader.account_ref})
            STOP.wait(control.meta('settings',{}).get('poll_seconds',3))
        except Exception as error:
            control.set_meta('runtime',{'status':'attention','message':'微信读取需要检查：'+type(error).__name__,'heartbeat':time.time()})
            reader=None;STOP.wait(10)
    progress.close();control.close()


def resolve_media(kernel):
    """Read incoming images as soon as they arrive, off the reply loop."""
    while not STOP.is_set():
        try:channel,messages,view,batch=MEDIA.get(timeout=1)
        except queue.Empty:continue
        try:
            current=kernel.control.get('channels',channel['id'])
            if any(m.get('media') for m in messages) and current.get('enabled') and current.get('allow_read'):
                reader=NativeReader(private_ids=(current['native_id'],) if current['kind']=='friend' else ())
                media=MediaMemory(kernel.control)
                messages=[{**media.resolve(reader,current,m),'_media_resolved':True} if m.get('media') else m for m in messages]
        except Exception:
            pass
        try:INBOX.put((channel,messages,view,batch),timeout=5)
        except queue.Full:pass


def drain(kernel):
    while not INBOX.empty():
        channel,messages,view,batch=INBOX.get_nowait()
        try:
            if any(m.get('media') and not m.get('_media_resolved') for m in messages):
                current=kernel.control.get('channels',channel['id'])
                if current.get('enabled') and current.get('allow_read'):
                    reader=NativeReader(private_ids=(current['native_id'],) if current['kind']=='friend' else ())
                    media=MediaMemory(kernel.control);messages=[media.resolve(reader,current,m) for m in messages]
            # A channel may have been archived while its batch was in flight.
            kernel.ingest(channel,messages,view)
        except KeyError:pass
        ACK.put((channel['native_id'],batch))


def process_job(control,kernel,job):
    body=json.loads(job['body']);kind=job['kind']
    if kind=='memory_journal':
        from episodic_memory import EpisodicMemory
        memory=EpisodicMemory(control,body['persona_id'],body['session_id'])
        try:
            provider=control.get('providers',control.meta('settings',{}).get('memory_provider_id','deepseek'))
            from provider_client import ProviderClient
            result=memory.consolidate(ProviderClient(provider,control.secret(provider['id'])),include_today=True)
            return {'message':'已整理一批每日记忆。' if result else '目前没有可整理的新增原文，或上一批整理尚在间隔内。','result':result}
        finally:memory.close()
    if kind in ('friend_requests','friend_add','friend_accept'):
        from friends import Friends
        if body.get('persona_id'):
            persona=control.get('personas',body['persona_id']);session=control.get('sessions',body['session_id']);channel=control.get('channels',body['channel_id'])
            if not all(persona.get(k) for k in ('enabled','tool_access')) or not session.get('enabled',True) or active_persona(session)!=persona['id'] or not channel.get('enabled') or not channel.get('allow_send') or channel['session_id']!=session['id']:
                raise PermissionError('发起好友操作的人设或来源聊天权限已关闭')
        friends=Friends(control,run_ui)
        result=friends.listing() if kind=='friend_requests' else friends.add(body['identifier'],body['verification']) if kind=='friend_add' else friends.accept(body['request_id'])
        if result['status'] in ('added','accepted','already_friend'):
            directory(control,friends.reader,control.meta('account')['self_sender_id'])
        return result
    if kind=='simulate':return kernel.simulate(body['session_id'],body['text'],body.get('persona_id'))
    if kind=='vision':return MediaMemory(control).retry(body['event_id'])
    if kind=='tool':
        session=control.get('sessions',body.get('session_id','main'));persona=control.get('personas',body.get('persona_id',active_persona(session)))
        if not persona.get('tool_access'):raise PermissionError('此人设的工具权限已关闭')
        provider=control.get('providers',control.meta('settings',{}).get('harness_provider_id','deepseek'));token=issue(control,provider['id'])
        try:return call('/harness/control.sock','/run',{'task_id':job['id'],'persona_id':persona['id'],'session_id':session['id'],'model':provider['model'],'context_window_tokens':provider['context_window_tokens'],'token':token,'prompt':body['prompt']},timeout=180)
        finally:revoke(control,token)
    if kind=='directory':
        reader=NativeReader();return {'message':'微信目录已刷新','count':len(directory(control,reader,control.meta('account',{}).get('self_sender_id','')))}
    if kind=='enroll_account':
        reader=NativeReader();old=control.meta('account',{})
        roots=active_account_roots(reader.identity)
        self_id=roots[0].parent.name.rsplit('_',1)[0]
        if not self_id.startswith('wxid_'):raise ValueError('无法确认当前账号的自身标识')
        if old.get('self_sender_id') and old['self_sender_id']!=self_id:raise ValueError('这些记忆属于另一个微信账号，请使用独立数据卷')
        control.set_meta('account',{'account_ref':reader.account_ref,'self_sender_id':self_id})
        control.set_meta('v2_live_ready',True);directory(control,reader,self_id)
        return {'message':'当前微信账号已绑定；请选择群或好友并完成窗口绑定'}
    if kind=='memory_delete':
        memory=kernel.scopes.memory(body['persona_id'],body['session_id'])
        try:
            with memory.db:memory.db.execute('DELETE FROM facts WHERE account=? AND group_id=? AND id=?',('shared',body['session_id'],body['id']))
        finally:memory.db.close()
        return {'message':'已删除这条记忆'}
    if kind=='say_in':
        target=control.get('channels',body['channel_id'])
        if not target.get('enabled') or not target.get('allow_send'):
            raise PermissionError('这个聊天已失去发送授权')
        text=(body.get('text') or '').strip()
        if not 1<=len(text)<=500:raise ValueError('跨聊天消息需要 1–500 字')
        account=control.meta('account')
        operation='sayin-'+hashlib.sha256((target['id']+text+str(time.time())).encode()).hexdigest()[:48]
        receipt=send(target['native_id'],text,operation,account['account_ref'],
                     time.time()+45,account.get('self_sender_id'))
        if receipt.get('status')=='sent':
            persona=control.get('personas',body['persona_id'])
            session_id=body.get('session_id') or target['session_id']
            confirmed=[dict(message_id=receipt.get('message_id') or operation,
                            sender_id='agent:'+persona['id'],agent_id=persona['id'],
                            display_name=persona['name'],channel_name=target['name'],
                            sent_at=datetime.now(timezone.utc).isoformat(),text=text)]
            conversation=kernel.scopes.conversation(session_id)
            try:conversation.append(session_id,confirmed)
            finally:conversation.db.close()
            from episodic_memory import EpisodicMemory
            archive=EpisodicMemory(control,persona['id'],session_id)
            try:archive.append(target,confirmed,'assistant')
            finally:archive.close()
            control.activity('reply',{'message':persona['name']+'：去“'+target['name']+'”说了 1 条','channel':target['name'],**receipt})
        return receipt
    if kind=='bind':
        channel=control.get('channels',body['channel_id'])
        entry=next((e for e in control.meta('directory',[]) if e['native_id']==channel['native_id']),None)
        if not entry:raise ValueError('聊天已不在微信目录，请刷新')
        receipt=run_ui('/bot/ui_admin.py',{'action':body.get('stage','search'),'row_top':body.get('row_top'),
            'selected':{**entry,'conversation_id':entry['native_id'],'account_ref':control.meta('account')['account_ref']}})
        if body.get('stage') in ('bind','auto'):
            entries=json.loads((ROOT/'ui'/'conversations.json').read_text())
            control.set_meta('bindings',list(entries))
        return receipt
    if kind=='moments_draft':return {'message':'朋友圈草稿已保存',**Moments(control).save(body['text'],body.get('visibility','private'),body.get('persona_id'))}
    if kind=='moments_publish':
        settings=control.meta('settings',{})
        if body.get('autonomous') and not all(settings.get(k) for k in ('enabled','moments_auto_publish','moments_enabled')):raise PermissionError('自主朋友圈已关闭')
        return Moments(control).publish(body['draft_id'],run_ui,body.get('prepare_only',False))
    if kind=='moments_think':
        persona=control.get('personas',body['persona_id']);return moments_schedule.think(control,body,kernel.client(persona),run_ui)
    if kind=='cancel':
        target=next((j for j in control.jobs() if j['id']==body['job_id']),None)
        if not target:raise ValueError('任务不存在')
        if target['state']=='running' and target['kind']=='tool':
            result=call('/harness/control.sock','/cancel',{'task_id':target['id']},timeout=10)
            control.finish_job(target['id'],'cancelled',{'message':'用户取消；已发生的文件改动保留'});return result
        if target['state']!='queued':raise ValueError('该任务当前不能取消')
        control.finish_job(target['id'],'cancelled',{'message':'用户取消'});return {'message':'已取消'}
    if kind=='approval':
        target=next((j for j in control.jobs() if j['id']==body['job_id']),None)
        if not target or target['state']!='approval' or target['kind']!='admin_command':raise ValueError('没有这项待审批操作')
        state='approved' if body.get('approved') else 'rejected'
        control.finish_job(target['id'],state,{'message':'已批准，等待本机管理员执行器' if state=='approved' else '管理员拒绝，未执行'})
        return {'message':'审批决定已保存'}
    raise ValueError('此任务处理器尚未接入：'+kind)


def background_tool(job):
    store=ControlStore(ROOT)
    try:return process_job(store,None,job)
    finally:store.close()


def tool_feedback(control,kernel,job,result):
    body=json.loads(job['body'])
    if not body.get('channel_id'):return
    try:
        channel=control.get('channels',body['channel_id']);session=control.get('sessions',body['session_id'])
        if not channel.get('enabled') or not channel.get('allow_send') or channel['session_id']!=session['id']:return
        conversation=kernel.scopes.conversation(session['id'])
        text=result.get('message') or result.get('error') or '任务结束'
        if job['kind']=='friend_requests':text+='\n'+json.dumps({'requests':result.get('requests',[])},ensure_ascii=False)
        conversation.append(session['id'],[dict(message_id='tool:'+job['id'],sender_id='tool',display_name='工具执行结果',channel_name=channel['name'],sent_at=datetime.now(timezone.utc).isoformat(),text=text[:2000])]);conversation.db.close()
        kernel.turns.resume(session,channel['id'],body['persona_id'])
        control.set_meta('trigger:'+session['id'],{'kind':'tool','at':time.time()})
    except KeyError:return # Configuration may have been archived while the job ran.


def main():
    os.umask(0o077);control=ControlStore(ROOT);legacy=json.loads(Path('/bot-config/config.json').read_text())
    if not control.meta('account') and legacy.get('account_ref'):
        control.set_meta('account',{'account_ref':legacy['account_ref'],'self_sender_id':legacy['self_sender_id']})
    preview=ROOT/'console-preview';preview.mkdir(exist_ok=True);preview.chmod(0o733)
    registry=ROOT/'ui'/'conversations.json'
    if registry.exists():control.set_meta('bindings',list(json.loads(registry.read_text())))
    # A previous running job may have crossed an external side-effect boundary.
    with control.db:control.db.execute("UPDATE jobs SET state='interrupted',result=? WHERE state='running'",(json.dumps({'message':'运行器重启，任务中断；请核对结果'}),))
    if not control.meta('single_account_mode_v1'):
        for session in control.list('sessions'):
            control.put('sessions',{**session,'active_persona_id':active_persona(session)})
        control.set_meta('settings',{**control.meta('settings',{}),'agent_dialogue_enabled':False})
        control.set_meta('single_account_mode_v1',True)
    counter=TokenCounter()
    from migrate_v1 import attribute_legacy_agent
    attribute_legacy_agent(control)
    from episodic_memory import backfill
    migrated=backfill(control)
    control.set_meta('archive_backfill',migrated)
    def sender(channel,content,operation,deadline):
        account=control.meta('account');return send(channel['native_id'],content,operation,account['account_ref'],deadline,account['self_sender_id'])
    broker=start_broker()
    classroom.recover(control)
    kernel=AgentKernel(control,counter,sender,tool_factory=lambda p,s,c,a:AgentTools(control,p,s,c,actor=a))
    kernel.before_send=lambda:drain(kernel)
    thread=threading.Thread(target=collect,daemon=True);thread.start()
    media_thread=threading.Thread(target=resolve_media,args=(kernel,),daemon=True);media_thread.start();last_learn=0;last_reminder=0;last_timetable=0;last_outbox=0
    pool=concurrent.futures.ThreadPoolExecutor(max_workers=1);running=None
    while not STOP.is_set() and not (ROOT/'STOP').exists():
        drain(kernel)
        if running and running[1].done():
            old,future=running;running=None
            try:result=future.result();state='done'
            except Exception as error:result={'error':str(error) if isinstance(error,RuntimeError) else type(error).__name__};state='failed'
            current=control.db.execute('SELECT state FROM jobs WHERE id=?',(old['id'],)).fetchone()
            if current and current['state']!='cancelled':control.finish_job(old['id'],state,result);tool_feedback(control,kernel,old,result)
        job=control.take_job(exclude=('tool',) if running else ())
        if job:
            try:
                if job['kind']=='tool':running=(job,pool.submit(background_tool,job))
                else:
                    result=process_job(control,kernel,job);control.finish_job(job['id'],'done',result)
                    if job['kind'] in ('moments_publish','friend_requests','friend_add','friend_accept'):tool_feedback(control,kernel,job,result)
            except Exception as error:
                control.finish_job(job['id'],'failed',{'error':str(error) if isinstance(error,(ValueError,PermissionError,KeyError,RuntimeError)) else type(error).__name__})
        if control.meta('settings',{}).get('enabled') and time.time()-last_reminder>=1:
            last_reminder=time.time()
            try:classroom.deliver(control,sender,last_reminder)
            except Exception as error:control.activity('error',{'message':'提醒发送异常：'+type(error).__name__})
        if control.meta('settings',{}).get('enabled') and time.time()-last_timetable>=3:
            last_timetable=time.time()
            try:timetable.tick(control,sender,last_timetable)
            except Exception as error:control.activity('error',{'message':'课前提醒异常：'+type(error).__name__})
        if control.meta('settings',{}).get('enabled') and time.time()-last_outbox>=5:
            last_outbox=time.time()
            try:outbox.deliver(control,sender,last_outbox)
            except Exception as error:control.activity('error',{'message':'补发异常：'+type(error).__name__})
        if control.meta('v2_live_ready',False):
            schedule(control,kernel.turns,time.time());moments_schedule.schedule(control,time.time())
        if control.meta('settings',{}).get('enabled') and control.meta('v2_live_ready',False):
            for session in control.list('sessions'):
                try:kernel.step(session['id'])
                except Exception as error:
                    kernel.turns.stop(session['id']);control.activity('error',{'message':'本轮未继续：'+type(error).__name__+' '+str(error)[:120],'session':session['id']})
            if time.time()-last_learn>300:
                for session in control.list('sessions'):
                    try:kernel.learn(session['id'])
                    except Exception as error:control.activity('memory_error',{'message':'记忆整理失败：'+type(error).__name__,'session':session['id']})
                last_learn=time.time()
        STOP.wait(.2)
    STOP.set();thread.join(timeout=15)
    if running:
        try:call('/harness/control.sock','/cancel',{'task_id':running[0]['id']},timeout=5)
        except OSError:pass
    pool.shutdown(wait=True,cancel_futures=True);broker.shutdown();control.close()


if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_:STOP.set());signal.signal(signal.SIGINT,lambda *_:STOP.set());main()
