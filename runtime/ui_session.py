"""Refresh main-window identity only after the native bound account was verified."""
import json
import subprocess
import sys
from pathlib import Path
from Xlib import X,display
from group_sender import ROOT
from ui_lock import Locked
from window_geometry import restore


def verify_request(request,registry,uid,exe,start_ticks):
    if (type(request.get('wechat_pid')) is not int or request['wechat_pid']<=0 or not request.get('account_ref')
        or not registry or {item.get('account_ref') for item in registry.values()}!={request['account_ref']}
        or uid!=['1000']*4 or exe!='wechat' or request.get('start_ticks')!=start_ticks):
        raise RuntimeError('绑定账号或微信进程不符，不能刷新窗口身份')


def refresh(request):
    with Locked(ROOT,timeout=10):
        pid=request['wechat_pid'];proc=Path('/proc')/str(pid)
        uid=next(line.split()[1:] for line in (proc/'status').read_text().splitlines() if line.startswith('Uid:'))
        registry=json.loads((ROOT/'conversations.json').read_text())
        start=(proc/'stat').read_text().rsplit(')',1)[1].split()[19]
        verify_request(request,registry,uid,(proc/'exe').readlink().name,start)
        connection=display.Display(':1');root=connection.screen().root
        try:
            result=subprocess.run(['xdotool','search','--class','^wechat$'],capture_output=True,text=True,timeout=5)
            windows=[]
            for raw in result.stdout.splitlines():
                try:
                    window=connection.create_resource_object('window',int(raw));g=window.get_geometry()
                    owner=window.get_full_property(connection.intern_atom('_NET_WM_PID'),X.AnyPropertyType)
                    if owner and int(owner.value[0])==pid and window.get_wm_class()==('wechat','wechat') and g.width>=600 and g.height>=400:
                        windows.append(window)
                except Exception:continue
            if len(windows)!=1:raise RuntimeError('绑定微信主窗口不唯一，不能自动刷新')
            window=windows[0]
            def run(*args):return subprocess.run(args,check=True,timeout=5)
            restore(window,root,run)
            path=ROOT/'verified-desktop.json';old=json.loads(path.read_text())
            value={**old,'wechat_pid':pid,'window_id':window.id}
            if value!=old:
                temporary=path.with_suffix('.session-new');temporary.write_text(json.dumps(value));temporary.replace(path)
            return {'status':'verified','identity_refreshed':value!=old,'window_id':window.id}
        finally:connection.close()


if __name__=='__main__':print(json.dumps(refresh(json.load(sys.stdin))))
