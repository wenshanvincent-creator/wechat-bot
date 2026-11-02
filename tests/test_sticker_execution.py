import copy
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from provider_client import ProviderClient,sticker_promise


class Client(ProviderClient):
    def __init__(self,outputs):
        super().__init__({'model':'synthetic'},'fake');self.outputs=iter(outputs);self.requests=[]
    def complete(self,messages,**options):
        self.requests.append((copy.deepcopy(messages),options))
        return {'content':json.dumps({'messages':next(self.outputs)},ensure_ascii=False)},{'prompt_tokens':1}


def test_promise_corrected_to_native_action_without_tools(monkeypatch):
    monkeypatch.setattr('provider_client.resolve',lambda name:({'native':True},None))
    client=Client([['表情我再试一次'],[{'sticker':'虚构笑脸'}]])
    output,usage=client.decision('虚构',[])
    assert output==[{'sticker':'虚构笑脸'}] and usage['prompt_tokens']==2
    assert not client.requests[-1][1].get('tools')


def test_repeated_promise_is_bounded_and_reports_no_background_retry():
    client=Client([['表情我再试一次']]*2)
    output,_=client.decision('虚构',[])
    assert output==['这轮没有发出表情，未安排后台重试'] and len(client.requests)==2


def test_context_reply_failure_and_real_default_emoji_are_not_retried():
    for output in (['这个表情很搞笑'],['表情没发出去'],['表情我再试一次[旺柴]'],[{'sticker':'笑脸'}]):
        assert not sticker_promise(output)
