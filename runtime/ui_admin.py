"""Operator-guided search result enrollment. Never sends a message."""
import base64
import fcntl
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path
from enroll_ui import bind_window
from group_sender import Conversation,ROOT
from ui_lock import Locked


def main():
    request=json.load(sys.stdin);action=request['action'];selected=request['selected']
    with Locked(ROOT,timeout=30):
        bind_window()
        pending=ROOT/'pending-group.json'
        if action in ('select','bind'):
            old=json.loads(pending.read_text())
            if old['conversation_id']!=selected['conversation_id'] or old['account_ref']!=selected['account_ref']:raise ValueError('绑定目标已变化，请重新搜索')
            selected=old
        app=Conversation(selected)
        try:
            app.check_window()
            if action in ('search','auto'):
                app.run('xdotool','windowactivate','--sync',str(app.window.id));time.sleep(.4)
                app.run('xdotool','mousemove','--window',str(app.window.id),'163','58','click','1');time.sleep(.3)
                box=app.capture((60,48,245,70))
                app.run('xdotool','key','--clearmodifiers','ctrl+a','BackSpace');time.sleep(.2)
                app.set_clipboard(selected['name']);app.run('xdotool','key','ctrl+v');time.sleep(1.2)
                if app.capture((60,48,245,70)).tobytes()==box.tobytes():
                    # The first paste can land before the search box takes focus.
                    app.set_clipboard(selected['name']);app.run('xdotool','key','ctrl+v');time.sleep(1.2)
                time.sleep(.8)
                if action=='auto':
                    # One fixed sequence: search, click the first result, bind.
                    # The first row under the matched section is the exact-name hit.
                    row=request.get('row_top') or 128
                    stream=io.BytesIO();app.capture((75,row-18,215,row+18)).save(stream,format='PNG')
                    selected['search_template_png']=base64.b64encode(stream.getvalue()).decode()
                    opened=app.capture((0,0,880,640))
                    app.run('xdotool','mousemove','--window',str(app.window.id),'150',str(row),'click','1');time.sleep(.8)
                    after=app.capture((0,0,880,640))
                    if after.tobytes()==opened.tobytes():raise RuntimeError('点击搜索结果没有打开任何聊天，请重试')
                    first=app.capture((310,38,610,74));time.sleep(.5);second=app.capture((310,38,610,74))
                    if first.tobytes()!=second.tobytes():raise RuntimeError('聊天标题还在变化，请重试')
                    selected['title_rect']=[310,38,300,36]
                    selected['title_hash']=hashlib.sha256(second.tobytes()).hexdigest()
                    file=ROOT/'conversations.json';entries=json.loads(file.read_text()) if file.exists() else {}
                    entries[selected['conversation_id']]=selected;file.write_text(json.dumps(entries,ensure_ascii=False))
            elif action=='select':
                row=request.get('row_top')
                if type(row) is not int or not 80<=row<=464:raise ValueError('请填写截图中搜索结果行顶部的位置')
                stream=io.BytesIO();app.capture((75,row,215,row+36)).save(stream,format='PNG')
                selected['search_template_png']=base64.b64encode(stream.getvalue()).decode()
                app.run('xdotool','mousemove','--window',str(app.window.id),'150',str(row+18),'click','1');time.sleep(.5)
            elif action=='bind':
                if 'search_template_png' not in selected:raise ValueError('请先选择搜索结果并核对聊天标题')
                selected['title_rect']=[310,38,300,36]
                selected['title_hash']=hashlib.sha256(app.capture((310,38,610,74)).tobytes()).hexdigest()
                file=ROOT/'conversations.json';entries=json.loads(file.read_text()) if file.exists() else {}
                entries[selected['conversation_id']]=selected;file.write_text(json.dumps(entries,ensure_ascii=False))
            else:raise ValueError('未知绑定阶段')
            pending.write_text(json.dumps(selected,ensure_ascii=False))
            folder=Path('/state/console-preview')
            app.capture((0,0,880,640)).save(folder/'desktop.png')
            os.chmod(folder/'desktop.png',0o644)
            print(json.dumps({'message':{'search':'搜索已打开，请按截图选择准确聊天','select':'已选中，请核对标题后确认绑定','bind':'聊天窗口已绑定','auto':'已绑定搜索结果第一项'}[action],'preview':'/api/desktop.png','stage':action}))
        finally:app.close()


if __name__=='__main__':main()
