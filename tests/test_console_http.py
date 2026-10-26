import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from console_server import Handler
from control_store import ControlStore


def test_real_http_login_read_and_csrf_protected_save(tmp_path):
    store=ControlStore(tmp_path);store.seed({'groups':[]},'测试人设','key-that-never-leaks');store.set_password('long-test-password');store.close()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.state_root=tmp_path
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base='http://127.0.0.1:'+str(server.server_port)
    def request(path,body=None,headers=None):
        return urllib.request.urlopen(urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json',**(headers or {})}))
    try:
        with pytest.raises(urllib.error.HTTPError) as error:request('/api/state')
        assert error.value.code==401
        login=request('/api/login',{'password':'long-test-password'});value=json.load(login)
        cookie=login.headers['Set-Cookie'].split(';')[0]
        assert 'HttpOnly' in login.headers['Set-Cookie']
        state=json.load(request('/api/state',headers={'Cookie':cookie}))
        assert 'key-that-never-leaks' not in json.dumps(state)
        with pytest.raises(urllib.error.HTTPError):request('/api/settings',state['settings'],{'Cookie':cookie})
        new={**state['settings'],'enabled':True}
        assert json.load(request('/api/settings',new,{'Cookie':cookie,'X-CSRF-Token':value['csrf']}))['enabled']
        with pytest.raises(urllib.error.HTTPError) as error:
            request('/api/settings',{'agent_dialogue_enabled':True},{'Cookie':cookie,'X-CSRF-Token':value['csrf']})
        assert error.value.code==400
        assert not json.load(request('/api/state',headers={'Cookie':cookie}))['settings']['agent_dialogue_enabled']
    finally:server.shutdown();server.server_close()
