"""Exercise actual compression using synthetic context and a reduced test budget."""
import json
import tempfile
from pathlib import Path

from conversation_context import ConversationContext, TokenCounter
from deepseek import decision_body
from model_view import GroupView


if __name__ == '__main__':
    config = json.loads(Path('/bot-config/config.json').read_text())
    secrets = dict(line.split('=',1) for line in Path('/bot-config/secrets.env').read_text().splitlines() if '=' in line)
    key = secrets['DEEPSEEK_API_KEY'].strip()
    try:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'synthetic-context.sqlite'
            counter = TokenCounter()
            context = ConversationContext(path,'synthetic-account',counter)
            messages = [dict(message_id=f'test:{i}',sender_id='test-member',
                sent_at='2026-10-25T06:00:00Z',
                text=f'第{i}条讨论：周六下午三点在学校体育馆打羽毛球，记得带球拍。') for i in range(60)]
            context.append('synthetic-group',messages)
            turns = [m for _,m in context.snapshot('synthetic-group')[2]]
            body = decision_body(config,GroupView().messages(turns),'',[])
            count = counter.count(body) + body['max_tokens']
            config['context_window_tokens'] = count * 2  # Trigger exactly at 50% without a huge paid request.
            recent,summary,receipt = context.prepare('synthetic-group',config,'',[],key)
            assert receipt['compressed'] and len(recent)==20
            assert receipt['estimated_tokens'] < receipt['threshold']
            assert '羽毛球' in summary and '三点' in summary
            context.db.close()
            reopened = ConversationContext(path,'synthetic-account',counter)
            assert reopened.snapshot('synthetic-group')[0] == summary
            assert len(reopened.snapshot('synthetic-group')[2])==20
            print(json.dumps({'status':'passed','real_hosted_summary':True,
                'reduced_test_window':config['context_window_tokens'],
                'production_window':1000000,'persisted':True,'wechat_messages_sent':0,**receipt}))
    except Exception as error:
        print(json.dumps({'status':'failed','error_type':type(error).__name__}))
        raise SystemExit(1) from None
