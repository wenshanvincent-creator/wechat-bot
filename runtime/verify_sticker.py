"""Real model-selected image test in the configured group, with durable receipts."""
import json
import time
import uuid
import argparse
from pathlib import Path

from bot import send
from deepseek import decide
from policy import Ledger
from reply_delivery import deliver
from sticker_catalog import catalog
from conversation_context import ConversationContext,TokenCounter


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--sticker',choices=[s['name'] for s in catalog()],default='爆笑_真的绷不住了')
    args=parser.parse_args()
    config=json.loads(Path('/bot-config/config.json').read_text())
    secrets=dict(line.split('=',1) for line in Path('/bot-config/secrets.env').read_text().splitlines() if '=' in line)
    messages=decide(config,[{'speaker':'测试者','text':'鲸鱼娘，来张表情，别发文字，用“'+args.sticker+'”这张。'}],
        Path('/bot-config/personality.md').read_text(),secrets['DEEPSEEK_API_KEY'].strip())
    assert messages==[{'sticker':args.sticker}]
    group=config['groups'][0];ledger=Ledger('/state/decisions.sqlite')
    assert ledger.can_send(group)
    operation='sticker-test-'+uuid.uuid4().hex
    ledger.record(operation,group,'checking',time.time(),'',config)
    receipt=deliver(ledger,group,operation,messages,config['account_ref'],send)
    print(json.dumps({'operation':operation,'model_selected':messages,**receipt}),flush=True)
    for row in ledger.db.execute('SELECT receipt FROM reply_parts WHERE operation=?',(operation,)):
        result=json.loads(row[0]);print(json.dumps(result),flush=True)
        if result.get('status')=='sent':
            context=ConversationContext('/state/context.sqlite',config['account_ref'],TokenCounter())
            context.append(group,[dict(message_id=result['message_id'],sender_id=config['self_sender_id'],
                sent_at=result['sent_at'],text='[表情：'+result['sticker']+']')])
    assert receipt['status']=='sent'
