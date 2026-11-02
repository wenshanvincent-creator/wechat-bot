"""Bounded restoration of an already enrolled desktop; never logs an account in."""
import threading
import time

_lock=threading.Lock()
_last_wake={}


def wake_bound_account(account_ref,identity):
    with _lock:
        now=time.monotonic()
        key=(account_ref,identity)
        if now-_last_wake.get(key,float('-inf'))<60:
            return False
        _last_wake[key]=now
        from bot import run_ui
        run_ui('/bot/ui_wake.py',{'account_ref':account_ref,'wechat_pid':identity[1]})
        return True


def live_ready(control,now=None):
    runtime=control.meta('runtime',{}) or {}
    account=control.meta('account',{}) or {}
    age=(time.time() if now is None else now)-runtime.get('heartbeat',0)
    return (runtime.get('status')=='ready' and 0<=age<=30
            and bool(account.get('account_ref'))
            and runtime.get('account_ref')==account['account_ref'])
