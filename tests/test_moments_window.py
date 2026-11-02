import ast
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def desktop(windows):
    tree=ast.parse((Path(__file__).resolve().parents[1]/'runtime/moments_ui.py').read_text())
    node=next(n for n in tree.body if getattr(n,'name',None)=='MomentsDesktop')
    calls=[]
    class Subprocess:
        @staticmethod
        def run(args,**kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0,stdout='\n'.join(str(w.id) for w in windows))
    namespace={'subprocess':Subprocess,'time':SimpleNamespace(sleep=lambda _:None),
               'MomentsWindowMissing':type('MomentsWindowMissing',(RuntimeError,),{}),
               'X':SimpleNamespace(AnyPropertyType=0,ZPixmap=2),
               'Image':SimpleNamespace(frombytes=lambda *args:args)}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'moments_ui.py','exec'),namespace)
    app=namespace['MomentsDesktop'].__new__(namespace['MomentsDesktop'])
    app.identity={'wechat_pid':1};app.window=None
    byid={w.id:w for w in windows}
    app.connection=SimpleNamespace(create_resource_object=lambda kind,wid:byid[wid],intern_atom=lambda name:name)
    captures=[]
    def capture(*args):captures.append(args);return SimpleNamespace(data=b'pixels')
    app.root=SimpleNamespace(translate_coords=lambda w,x,y:SimpleNamespace(x=w.x,y=w.y),get_image=capture)
    return app,calls,captures


def test_transient_window_remap_reuses_only_a_verified_unique_candidate():
    app,calls,captures=desktop([window()]);missing=app.owned.__func__.__globals__['MomentsWindowMissing']
    original=app._owned_once;attempts=[]
    def remapped():
        attempts.append(1)
        if len(attempts)<3:raise missing('Qt remapping')
        return original()
    app._owned_once=remapped
    assert app.owned().id==1 and len(attempts)==3


def test_window_remap_wait_is_bounded_and_never_clicks_without_a_candidate():
    app,calls,captures=desktop([])
    with pytest.raises(RuntimeError):app.click(280,307)
    assert len(calls)==4 and not captures and not any('click' in c for c in calls)


def window(wid=1,pid=1,x=393,y=114,width=440,height=560):
    return SimpleNamespace(id=wid,x=x,y=y,get_geometry=lambda:SimpleNamespace(width=width,height=height),
        get_full_property=lambda *args:SimpleNamespace(value=[pid]),get_wm_class=lambda:('wechat','wechat'))


@pytest.mark.parametrize('x,y',[(393,114),(420,120),(70,30)])
def test_drifted_window_capture_and_click_follow_its_live_position(x,y):
    w=window(x=x,y=y);app,calls,captures=desktop([w])
    assert app.owned() is w
    app.capture((57,17,76,34));assert captures[-1][:4]==(x+57,y+17,19,17)
    app.click(160,433)
    assert calls[-1]==['xdotool','mousemove','--window','1','160','433','click','1']
    # The same handle moves again between operations; no cached screen origin.
    w.x+=20;app.capture((0,0,10,10));assert captures[-1][:2]==(x+20,y)


def test_window_identity_ambiguity_or_wrong_owner_cannot_be_clicked():
    for candidates in ([window(pid=2)],[window(width=880,height=640)],[window(),window(wid=2)]):
        app,calls,captures=desktop(candidates)
        with pytest.raises(RuntimeError,match='没有唯一|不唯一'):app.click(160,433)
        assert not captures and not any('click' in call for call in calls)


@pytest.mark.parametrize('ambiguous',[False,True])
def test_reopen_uses_relative_main_toolbar_and_waits_for_unique_window(tmp_path,ambiguous):
    w=window();main=window(wid=10,x=200,y=80,width=880,height=640)
    app,calls,captures=desktop([w]);scope=app.open.__func__.__globals__
    (tmp_path/'verified-desktop.json').write_text('{"wechat_pid":1}')
    ticks=itertools.count(0,.1)
    scope.update(ROOT=tmp_path,json=json,time=SimpleNamespace(monotonic=lambda:next(ticks),sleep=lambda _:None),
        Conversation=lambda _:SimpleNamespace(window=main,close=lambda:None),restore_main=lambda *args:None)
    app.connection.create_resource_object=lambda kind,wid:main if wid==10 else w
    attempts=[]
    def owned():
        attempts.append(1)
        if ambiguous:raise RuntimeError('窗口不唯一')
        if len(attempts)<3:raise scope['MomentsWindowMissing']('not open yet')
        return w
    app.owned=owned;checks=[];app.wait_check=lambda name:checks.append(name);app.check=lambda name:checks.append(name)
    if ambiguous:
        with pytest.raises(RuntimeError,match='不唯一'):app.open()
        assert not calls
    else:
        app.open()
        assert calls[0]==['xdotool','windowactivate','--sync','10','mousemove','--window','10','30','259','click','1']
        assert len(attempts)>=3 and checks==['camera','cancel','visibility']
        assert calls[-1][2:7]==['--window','1','66','25','mousedown']
