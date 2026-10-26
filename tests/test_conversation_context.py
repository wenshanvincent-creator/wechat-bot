import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from conversation_context import ConversationContext
from deepseek import decision_body


class Counter:
    def count(self, body):
        return sum(len(m['content']) for m in body['messages'])


def events(count=60):
    return [dict(message_id=str(i),sender_id='member',sent_at='2026-10-25T06:00:00Z',
                 text=f'{i}: ' + '讨论羽毛球约定。' * 30) for i in range(count)]


def setup_context(tmp_path):
    context = ConversationContext(tmp_path/'context.sqlite','account-a',Counter())
    context.append('group-a',events())
    messages = [m for _,m in context.snapshot('group-a')[2]]
    from model_view import GroupView
    config = dict(model='deepseek-flash',context_compress_ratio=0.5)
    before = Counter().count(decision_body(config,GroupView().messages(messages),'',[])) + 4096
    config['context_window_tokens'] = before * 2
    return context, config


def test_fifty_percent_boundary_compacts_and_keeps_recent_twenty(tmp_path):
    context,config = setup_context(tmp_path)
    messages,summary,receipt = context.prepare('group-a',config,'',[], '',
        lambda c,older,prior,key: '早期讨论羽毛球约定。')
    assert receipt['compressed'] and receipt['before_tokens'] == receipt['threshold']
    assert receipt['estimated_tokens'] < receipt['threshold']
    assert len(messages) == 20 and messages[0]['text'].startswith('40: ')
    assert '羽毛球' in summary
    reopened = ConversationContext(tmp_path/'context.sqlite','account-a',Counter())
    reopened.append('group-a',events())  # Old synchronized history must not reappear.
    assert len(reopened.snapshot('group-a')[2]) == 20
    assert reopened.snapshot('group-a')[0] == summary
    reopened.append('group-b',[])
    assert reopened.snapshot('group-b')[0] == ''
    other = ConversationContext(tmp_path/'context.sqlite','account-b',Counter())
    other.append('group-a',[])
    assert other.snapshot('group-a')[2] == []


def test_below_threshold_does_not_request_summary(tmp_path):
    context,config = setup_context(tmp_path)
    config['context_window_tokens'] += 2
    _,_,receipt = context.prepare('group-a',config,'',[], '',
                                 lambda *args: pytest.fail('Premature compression'))
    assert not receipt['compressed']


def test_failed_summary_preserves_raw_history(tmp_path):
    context,config = setup_context(tmp_path)
    before = context.snapshot('group-a')
    def fail(*args):
        raise RuntimeError('API unavailable')
    with pytest.raises(RuntimeError):
        context.prepare('group-a',config,'',[], '',fail)
    assert context.snapshot('group-a') == before


def test_usage_calibration_includes_server_overhead(tmp_path):
    context,config = setup_context(tmp_path)
    body = decision_body(config, [], '', [])
    context.record_usage('group-a',body,{'prompt_tokens':Counter().count(body)+100})
    assert context.snapshot('group-a')[1] == 100
