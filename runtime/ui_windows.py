"""Read-only owned-window diagnostic, no message bodies."""
import json
import os
import subprocess
from Xlib import X,display
os.environ['DISPLAY']=':1'
connection=display.Display();root=connection.screen().root
out=[]
for raw in subprocess.check_output(['xdotool','search','--onlyvisible','--class','wechat'],text=True).splitlines():
    w=connection.create_resource_object('window',int(raw))
    g=w.get_geometry();p=root.translate_coords(w,0,0)
    pid=w.get_full_property(connection.intern_atom('_NET_WM_PID'),X.AnyPropertyType)
    out.append({'window':w.id,'class':w.get_wm_class(),'pid':int(pid.value[0]) if pid else None,'rect':[p.x,p.y,g.width,g.height],'title':w.get_wm_name()})
print(json.dumps(out,ensure_ascii=False,default=lambda x:x.decode('utf-8',errors='replace')));connection.close()
