import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import deepseek


@pytest.mark.parametrize('decision,expected', [({'messages': []}, []),
                                              ({'messages': ['hello','again']}, ['hello','again'])])
def test_hosted_decision_request_and_silence(monkeypatch, decision, expected):
    def open_url(request, timeout):
        body = json.loads(request.data)
        assert body['thinking'] == {'type': 'disabled'}
        assert body['response_format'] == {'type': 'json_object'}
        assert body['messages'][1]['role'] == 'user'
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(decision)}}]}).encode())
    monkeypatch.setattr(deepseek.urllib.request, 'urlopen', open_url)
    assert deepseek.decide({'model': 'deepseek-flash'},
                          [{'sender': 'person', 'text': 'Hello'}], 'Test role', 'not-a-real-key') == expected


@pytest.mark.parametrize('decision', [{'messages': 'hello'},
                                     {'messages': ['hello','']}])
def test_invalid_model_decisions_do_not_become_sendable_text(monkeypatch, decision):
    monkeypatch.setattr(deepseek.urllib.request, 'urlopen', lambda *args, **kwargs:
        io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(decision)}}]}).encode()))
    with pytest.raises(ValueError):
        deepseek.decide({'model': 'deepseek-flash'}, [], '', 'not-a-real-key')


def test_recalled_memory_is_passed_as_conversation_data(monkeypatch):
    memories = ['群友甲喜欢羽毛球']
    def open_url(request, timeout):
        body = json.loads(request.data)
        assert '本群记忆：\n群友甲喜欢羽毛球' in body['messages'][1]['content']
        assert '喜欢羽毛球' not in body['messages'][0]['content']
        return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps({'messages':[]})}}]}).encode())
    monkeypatch.setattr(deepseek.urllib.request, 'urlopen', open_url)
    assert deepseek.decide({'model': 'deepseek-flash'},
                          [], 'Test role', 'not-a-real-key', memories) == []
