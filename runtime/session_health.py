"""One persisted native-session health record and bounded component retries.

Only derived identity metadata is recorded: no chat text, keys or memory data.
An absent database descriptor is UNAVAILABLE, never proof of a real logout.
"""
import time


def blocked_send_jobs(control):
    from native_recovery import live_ready
    return () if live_ready(control) else ('moments_think','moments_publish','say_in','voice','friend_add','friend_accept','friend_requests')


def error_code(error):
    name=type(error).__name__
    if name=='NativeAccountUnavailable':return 'account_unavailable'
    if name=='SnapshotChanged':return 'snapshot_busy'
    if name=='SessionUIError':
        cause=str(error.__cause__ or '')
        if '主窗口不唯一' in cause:return 'ui_main_window_ambiguous'
        if '绑定账号或微信进程不符' in cause:return 'ui_identity_mismatch'
        return 'ui_unavailable'
    text=str(error)
    if '绑定账号不同' in text or 'account changed' in text.lower():return 'account_changed'
    if name=='ProcessLookupError' or 'process changed' in text.lower() or 'Expected one WeChat main process' in text:
        return 'process_unavailable'
    return name


def diagnostics():
    from native_keys import process_identity,active_account_roots
    try:
        identity=process_identity()
        return {'process_identity':list(identity),'active_root_count':len(active_account_roots(identity))}
    except (RuntimeError,OSError) as error:
        return {'process_identity':None,'active_root_count':None,'process_error':type(error).__name__}


class SessionUIError(RuntimeError):
    pass


class FailureGate:
    def __init__(self,control,component,delays=(2,5,10,20,30,60)):
        self.control=control;self.component=component;self.key='fault:'+component;self.delays=delays

    def due(self,now=None):
        return (time.time() if now is None else now)>=(self.control.meta(self.key,{}) or {}).get('next_retry',0)

    def fail(self,error,now=None):
        now=time.time() if now is None else now
        old=self.control.meta(self.key,{}) or {};code=error_code(error)
        # One outage can alternate between a missing DB and a failed UI wake.
        # A changed exception type must not reset the backoff or open new alarms.
        same=bool(old.get('active'))
        count=old.get('count',0)+1 if same else 1
        delay=self.delays[min(count-1,len(self.delays)-1)]
        value={'active':True,'code':code,'count':count,'first_seen':old['first_seen'] if same else now,
               'last_seen':now,'next_retry':now+delay,'retry_seconds':delay}
        self.control.set_meta(self.key,value)
        if not same or (code=='account_changed' and old.get('code')!='account_changed'):
            self.control.activity('component_fault',{'message':self.component+' 暂停，自动退避检查：'+code,'component':self.component,**value})
        return delay

    def success(self,now=None):
        old=self.control.meta(self.key,{}) or {}
        if old.get('active'):
            self.control.set_meta(self.key,{**old,'active':False,'next_retry':0,'recovered_at':time.time() if now is None else now})
            self.control.activity('component_recovered',{'message':self.component+' 已恢复','component':self.component,'failed_checks':old['count']})


class SessionHealth:
    def __init__(self,control):
        self.control=control;self.gate=FailureGate(control,'native_session')

    def _record(self,state,message,details=None,now=None):
        now=time.time() if now is None else now
        old=self.control.meta('native_session',{}) or {}
        value={**old,**(details or {}),'state':state,'heartbeat':now,'message':message}
        if state=='ready':
            value['generation']=old.get('generation',0)+(old.get('state')!='ready' or old.get('process_identity')!=value.get('process_identity'))
        changed=old.get('state')!=state or old.get('process_identity')!=value.get('process_identity')
        if changed:
            value['changed_at']=now
            self.control.activity('native_session',{'message':message,'from':old.get('state'),'to':state,
                **{k:value[k] for k in ('process_identity','active_root_count','generation','error_code') if k in value}})
        self.control.set_meta('native_session',value)
        self.control.set_meta('runtime',{'status':'ready' if state=='ready' else 'attention','session_state':state,
            'message':message,'heartbeat':now,'account_ref':value.get('account_ref'),'generation':value.get('generation',0)})

    def start(self):
        self._record('recovering','正在核对绑定账号、重建读取器和窗口身份')

    def ready(self,reader):
        self.gate.success()
        self._record('ready','微信账号与窗口已核对；自动发送由总开关控制',
            {'process_identity':list(reader.identity),'active_root_count':1,'account_ref':reader.account_ref,'error_code':None,'process_error':None})

    def unbound(self):
        self._record('unbound','微信已登录，请先在控制台绑定当前账号')

    def read_busy(self,reader):
        # SQLCipher/WAL snapshot races are data contention, not a lost session.
        # Verify process/account metadata without decrypting another snapshot.
        reader.assert_active_account()
        old=self.control.meta('native_session',{}) or {}
        self.ready(reader)
        value=self.control.meta('native_session',{})
        self.control.set_meta('native_session',{**value,'snapshot_busy_count':old.get('snapshot_busy_count',0)+1,'last_snapshot_busy_at':time.time()})

    def failed(self,error):
        if error_code(error)=='snapshot_busy':return .3
        delay=self.gate.fail(error)
        code=error_code(error)
        details=diagnostics()
        state={'account_unavailable':'unavailable','process_unavailable':'unavailable',
               'account_changed':'account_changed'}.get(code,'ui_unavailable' if code.startswith('ui_') else 'attention')
        if details.get('active_root_count')==0 and state not in ('account_changed',):state='unavailable'
        message=('绑定账号已变化，已暂停发送，请核对账号' if state=='account_changed' else
                 '微信会话暂不可用，相关任务已暂停；正在退避检查（'+code+'）')
        self._record(state,message,{**details,'error_code':code})
        return delay
