import copy
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from search_contract import SearchContract
from provider_client import ProviderClient
from agent_tools import AgentTools
from control_store import ControlStore


@pytest.mark.parametrize('text,required',[
    ('搜一条新闻，然后一分钟后搜天气',{'instant','schedule'}),
    ('很好，搜的新闻呢，以及一分钟后记得搜个天气',{'instant','schedule'}),
    ('？搜了没',{'instant'}),('帮我搜明天的天气',{'instant'}),
    ('搜最近三天的新闻',{'instant'}),('明天八点搜上海天气',{'schedule'}),
    ('不要搜了',set()),('搜索功能正常吗',set()),('你好',set()),
])
def test_current_request_intent_does_not_confuse_forecast_date_with_schedule(text,required):
    assert SearchContract(text).required==required


class Client(ProviderClient):
    def __init__(self,responses):
        super().__init__({'model':'synthetic'},'fake-key');self.responses=iter(responses);self.requests=[]
    def complete(self,messages,**options):
        self.requests.append((copy.deepcopy(messages),options));return next(self.responses),{'prompt_tokens':1,'completion_tokens':1}


def answer(text):return {'content':json.dumps({'messages':[text]},ensure_ascii=False)}
def call(name,args=None):return {'role':'assistant','tool_calls':[{'id':name,'function':{'name':name,'arguments':json.dumps(args or {})}}]}
RESULT={'results':[{'title':'官方公告','summary':'发布说明','link':'https://example.com/news'}]}


class Tools:
    def __init__(self,text):
        self.search_contract=SearchContract(text);self.calls=[]
        self.schemas=[{'type':'function','function':{'name':n}} for n in ('web_search','class_search_schedule','send_voice')]
    def execute(self,name,args):
        self.calls.append(name)
        result=RESULT if name=='web_search' else {'ok':True,'id':'task1','run_at':'2026-10-29 08:00','channel':'test'}
        self.search_contract.record(name,result);return result


def test_promise_is_corrected_in_same_tool_conversation_and_returns_sources():
    tools=Tools('搜新闻');c=Client([answer('搜着呢爸'),call('web_search'),answer('公告发布说明 https://example.com/news')])
    output,usage=c.decision('虚构人设',[],tool_runner=tools)
    assert output==['公告发布说明 https://example.com/news'] and tools.calls==['web_search']
    assert usage['prompt_tokens']==3
    assert all(s['function']['name']!='send_voice' for s in c.requests[1][1]['tools'])


def test_successful_search_is_not_replayed_to_fix_an_empty_final_promise():
    tools=Tools('搜新闻');c=Client([call('web_search'),answer('本鱼这就搜'),answer('官方公告 https://example.com/news')])
    out,_=c.decision('虚构',[],tool_runner=tools)
    assert tools.calls==['web_search'] and 'example.com' in out[0]
    assert any(m['role']=='tool' for m in c.requests[-1][0])


def test_news_and_delayed_weather_are_independently_verified():
    tools=Tools('搜新闻，然后一分钟后搜天气')
    c=Client([call('web_search'),answer('新闻 https://example.com/news，天气等会搜'),
              call('class_search_schedule'),answer('新闻 https://example.com/news，定时搜索已创建 2026-10-29 08:00')])
    out,_=c.decision('虚构',[],tool_runner=tools)
    assert tools.calls==['web_search','class_search_schedule'] and '08:00' in out[0]


def test_persistent_nonexecution_returns_honest_failure_with_bounded_calls():
    tools=Tools('一分钟后搜天气');c=Client([answer('马上搜')]*3)
    out,_=c.decision('虚构',[],tool_runner=tools)
    assert len(c.requests)==3 and tools.calls==[] and '未创建成功' in out[0]


def test_search_failure_cannot_be_described_as_still_running():
    contract=SearchContract('搜新闻');contract.record('web_search',error=TimeoutError())
    assert contract.issue(['正在搜，稍后发你'])
    assert not contract.issue(['这次联网搜索失败，没拿到结果'])
    assert '失败' in contract.fallback()[0]


def test_required_location_can_be_clarified_without_creating_a_guess_task():
    contract=SearchContract('一分钟后搜天气')
    assert not contract.issue(['查哪个城市的天气？'])
    assert contract.events==[]


def test_correction_phase_rejects_other_mutations_even_if_model_calls_them():
    tools=Tools('搜新闻');c=Client([answer('我去搜'),call('send_voice'),call('web_search'),answer('https://example.com/news')])
    c.decision('虚构',[],tool_runner=tools)
    assert tools.calls==['web_search']


def real_tools(tmp_path):
    c=ControlStore(tmp_path);c.seed({'groups':['group@chatroom']},'虚构人设','fake-key')
    c.set_meta('monitor',{'sender_id':'owner'})
    p=c.get('personas','whale');p['tool_access']=True
    t=AgentTools(c,p,c.get('sessions','main'),c.get('channels','local-0'),actor='owner')
    t.configure_search('搜新闻，然后一分钟后搜天气');return c,t


def test_real_tools_deduplicate_search_and_log_derived_metadata_only(tmp_path,monkeypatch):
    c,t=real_tools(tmp_path);calls=[]
    monkeypatch.setattr('public_web.search',lambda *args:(calls.append(args) or RESULT['results']))
    for _ in range(2):t.execute('web_search',{'query':'PRIVATE-QUERY'})
    assert len(calls)==1 and t.actions==['web_search']
    logs=[r[0] for r in c.db.execute("SELECT body FROM activity WHERE kind='search_tool'")]
    assert len(logs)==1 and 'PRIVATE-QUERY' not in logs[0] and 'result_count' in logs[0]
    c.close()


def test_schedule_is_persisted_once_and_nonowner_cannot_create(tmp_path):
    c,t=real_tools(tmp_path);when=time.strftime('%Y-%m-%d %H:%M',time.localtime(time.time()+3600))
    args={'query':'上海天气','when':when,'repeat':'none'}
    first=t.execute('class_search_schedule',args);second=t.execute('class_search_schedule',dict(args))
    row=c.db.execute('SELECT body FROM reminders WHERE id=?',(first['id'],)).fetchone()
    assert first['id']==second['id'] and json.loads(row[0])['kind']=='search'
    assert c.db.execute('SELECT count(*) FROM reminders').fetchone()[0]==1
    t.actor='other';t._search_cache.clear()
    with pytest.raises(PermissionError):t.execute('class_search_schedule',{**args,'query':'北京天气'})
    c.close()
