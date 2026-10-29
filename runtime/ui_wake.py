"""Restore only the verified, enrolled WeChat window without opening a chat."""
import json
import sys

from group_sender import ROOT,Conversation,restore_main
from ui_lock import Locked


def wake(request):
    with Locked(ROOT,timeout=5):
        identity=json.loads((ROOT/'verified-desktop.json').read_text())
        registry=json.loads((ROOT/'conversations.json').read_text())
        if (identity['wechat_pid']!=request.get('wechat_pid') or not request.get('account_ref')
            or not any(item.get('account_ref')==request['account_ref'] for item in registry.values())):
            raise RuntimeError('微信唤醒的进程或绑定账号不匹配')
        desktop=Conversation({})
        try:
            restore_main(desktop.connection,desktop.root,desktop.window,desktop.pid)
            desktop.run('xdotool','windowactivate','--sync',str(desktop.window.id))
            return {'status':'restored'}
        finally:desktop.connection.close()


if __name__=='__main__':
    print(json.dumps(wake(json.load(sys.stdin))))
