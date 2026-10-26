"""Real hosted API test using synthetic data; never reads or sends WeChat messages."""
import json
import tempfile
from pathlib import Path

from group_memory import GroupMemory


if __name__ == '__main__':
    config = json.loads(Path('/bot-config/config.json').read_text())
    secrets = dict(line.split('=', 1) for line in Path('/bot-config/secrets.env').read_text().splitlines() if '=' in line)
    key = secrets['DEEPSEEK_API_KEY'].strip()
    try:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'synthetic-memory.sqlite'
            memory = GroupMemory(path, 'synthetic-account')
            def event(mid, text):
                return dict(message_id=mid, sender_id='test-member',
                            sent_at='2026-10-25T14:00:00+08:00', text=text)
            memory.enqueue('synthetic-group', [event('test:one', '我最喜欢的运动是羽毛球，请记住这个偏好。')])
            first = memory.consolidate('synthetic-group', config, key, 1000)
            assert first['stored'] > 0, 'No preference was extracted'
            memory.enqueue('synthetic-group', [event('test:two', '更正一下：我现在最喜欢的是网球，不是羽毛球，请更新之前的偏好。')])
            second = memory.consolidate('synthetic-group', config, key, 1400)
            memory.db.close()
            reopened = GroupMemory(path, 'synthetic-account')
            recalled = reopened.recall('synthetic-group', [event('query', '我最喜欢什么运动？')])
            assert any('网球' in m['fact'] and 'test:two' in m['source_ids'] for m in recalled)
            assert reopened.all('another-group') == []
            print(json.dumps({'status': 'passed', 'provider': 'DeepSeek', 'plugin': 'langmem',
                              'first': first, 'second': second, 'persisted_recall': True,
                              'cross_group_isolation': True, 'wechat_messages_sent': 0}))
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__}))
        raise SystemExit(1) from None
