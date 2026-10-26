"""Bind only the current owned WeChat window and the selected native group."""
import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import time
from pathlib import Path

from Xlib import X, display
from group_sender import Conversation,restore_main

os.environ['DISPLAY'] = ':1'
ROOT = Path('/state/ui')


def bind_window():
    pid = int(subprocess.check_output(['pgrep', '-xo', 'wechat'], text=True))
    connection = display.Display(':1')
    desktop = connection.screen().root
    windows = []
    for raw in subprocess.check_output(['xdotool', 'search', '--class', '^wechat$'], text=True).splitlines():
        window = connection.create_resource_object('window', int(raw))
        owner = window.get_full_property(connection.intern_atom('_NET_WM_PID'), X.AnyPropertyType)
        geo = window.get_geometry()
        pos = desktop.translate_coords(window, 0, 0)
        if owner and int(owner.value[0]) == pid and geo.width>=800 and geo.height>=600 and not window.get_wm_transient_for():
            windows.append(window)
    if len(windows) != 1:
        raise RuntimeError('请先登录微信并关闭设置窗口；未找到唯一的主窗口')
    window=windows[0];restore_main(connection,desktop,window,pid)
    geo=window.get_geometry();pos=desktop.translate_coords(window,0,0)
    if (pos.x,pos.y,geo.width,geo.height)!=(200,80,880,640):
        subprocess.run(['xdotool','windowsize','--sync',str(window.id),'880','640','windowmove','--sync',str(window.id),'200','80'],check=True,timeout=10)
        time.sleep(.3)
    value = {'wechat_pid': pid, 'window_id': windows[0].id, 'geometry': [200,80,880,640]}
    (ROOT / 'verified-desktop.json').write_text(json.dumps(value), encoding='utf-8')
    connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['search', 'select', 'bind'])
    parser.add_argument('--row-top', type=int)
    args = parser.parse_args()
    config = json.loads(Path('/bot-config/config.json').read_text())
    group = json.loads((ROOT / 'pending-group.json').read_text())
    if group['account_ref'] != config['account_ref'] or group['conversation_id'] not in config['groups']:
        raise ValueError('Pending group is outside the configured account/groups')
    bind_window()
    app = Conversation(group)
    try:
        if args.action == 'search':
            app.run('xdotool', 'windowactivate', '--sync', str(app.window.id))
            app.check_window()
            app.run('xdotool', 'mousemove', '--window', str(app.window.id), '163', '58', 'click', '1')
            app.run('xdotool', 'key', '--clearmodifiers', 'ctrl+a')
            app.set_clipboard(group['name'])
            app.run('xdotool', 'key', '--clearmodifiers', 'ctrl+v')
        elif args.action == 'select':
            if args.row_top is None or not 80 <= args.row_top <= 464:
                raise ValueError('Select requires an observed row top within the search pane')
            image = app.capture((75,args.row_top,215,args.row_top+36))
            stream = io.BytesIO(); image.save(stream,format='PNG')
            group['search_template_png'] = base64.b64encode(stream.getvalue()).decode()
            (ROOT / 'pending-group.json').write_text(json.dumps(group,ensure_ascii=False),encoding='utf-8')
            app.run('xdotool','mousemove','--window',str(app.window.id),'150',str(args.row_top+18),'click','1')
        else:
            # Operator has inspected the actual selected group title before binding.
            group['title_rect'] = [310,38,300,36]
            group['title_hash'] = hashlib.sha256(app.capture((310,38,610,74)).tobytes()).hexdigest()
            registry=ROOT/'conversations.json'
            entries=json.loads(registry.read_text()) if registry.exists() else {}
            entries[group['conversation_id']]=group
            registry.write_text(json.dumps(entries,ensure_ascii=False),encoding='utf-8')
            print(json.dumps({'enrolled_group':group['name'],'account_ref':group['account_ref']},ensure_ascii=False))
    finally:
        app.close()
