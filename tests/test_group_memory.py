import sys
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from group_memory import GroupMemory


def message(mid='m1', text='我喜欢打羽毛球'):
    return dict(message_id=mid, sender_id='member-a', sent_at='2026-10-25T10:00:00+08:00', text=text)


def extracted(mid='m1', fact='member-a 喜欢打羽毛球'):
    return [dict(id='fact-1', content=dict(subject_id='member-a', fact=fact, source_ids=[mid]))]


def test_persistent_recall_is_scoped_to_account_and_group(tmp_path):
    path = tmp_path / 'memory.sqlite'
    memory = GroupMemory(path, 'account-a')
    memory.enqueue('group-a', [message()])
    memory.consolidate('group-a', {}, '', 1000, lambda *args: extracted())
    restarted = GroupMemory(path, 'account-a')
    assert restarted.recall('group-a', [message(text='羽毛球打得怎样')])[0]['fact'].endswith('喜欢打羽毛球')
    assert restarted.recall('group-b', [message()]) == []
    assert GroupMemory(path, 'account-b').recall('group-a', [message()]) == []
    restarted.enqueue('group-a', [message()])
    assert restarted.consolidate('group-a', {}, '', 1400, lambda *args: pytest.fail('Replayed event')) is None


def test_corrections_update_existing_memory(tmp_path):
    memory = GroupMemory(tmp_path / 'memory.sqlite', 'account-a')
    memory.enqueue('group-a', [message()])
    memory.consolidate('group-a', {}, '', 1000, lambda *args: extracted())
    memory.enqueue('group-a', [message('m2', '我现在更喜欢网球')])
    memory.consolidate('group-a', {}, '', 1400,
                       lambda *args: extracted('m2', 'member-a 现在更喜欢网球'))
    assert len(memory.all('group-a')) == 1
    assert memory.all('group-a')[0]['content']['source_ids'] == ['m2']


def test_invalid_evidence_preserves_queue_and_limits_api_retry(tmp_path):
    memory = GroupMemory(tmp_path / 'memory.sqlite', 'account-a')
    memory.enqueue('group-a', [message()])
    with pytest.raises(ValueError, match='evidence'):
        memory.consolidate('group-a', {}, '', 1000, lambda *args: extracted('other-group-id'))
    assert memory.all('group-a') == []
    assert memory.consolidate('group-a', {}, '', 1001, lambda *args: pytest.fail('Early API retry')) is None
    assert memory.consolidate('group-a', {}, '', 1400, lambda *args: extracted())['processed'] == 1


def test_long_running_memory_retains_old_facts_but_extraction_context_is_bounded(tmp_path):
    memory=GroupMemory(tmp_path/'memory.sqlite','account-a')
    with memory.db:
        memory.db.executemany('INSERT INTO facts VALUES (?,?,?,?,?)',[
            ('account-a','group-a','old-'+str(i),json.dumps(dict(subject_id='member-a',fact='长期偏好'+str(i),source_ids=['old-source-'+str(i)])),i) for i in range(100)])
    memory.enqueue('group-a',[message()])
    def extractor(config,key,messages,existing,view):
        assert len(existing)==40
        return extracted()
    result=memory.consolidate('group-a',{},'',1000,extractor)
    assert result['stored']==101
    assert any(row['id']=='old-0' for row in memory.all('group-a'))
