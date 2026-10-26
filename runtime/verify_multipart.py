"""Explicit two-bubble test in the one configured group; really sends messages."""
import json
import time
import uuid
from pathlib import Path

from bot import send
from native_reader import NativeReader
from policy import Ledger
from reply_delivery import deliver


if __name__ == '__main__':
    config=json.loads(Path('/bot-config/config.json').read_text())
    group=config['groups'][0]
    reader=NativeReader()
    assert reader.account_ref==config['account_ref']
    ledger=Ledger('/state/decisions.sqlite')
    if not ledger.can_send(group):
        raise RuntimeError('Existing delivery needs review before testing')
    operation='multipart-test-'+uuid.uuid4().hex
    texts=['分条发送测试：这是第一条。','分条发送测试：这是第二条。']
    ledger.record(operation,group,'checking',time.time(),'',config)
    receipt=deliver(ledger,group,operation,texts,config['account_ref'],send)
    print(json.dumps({'operation':operation,**receipt}),flush=True)
    found=[m for m in reader.read_messages(group,limit=10)['messages']
           if m['sender_id']==config['self_sender_id'] and m['text'] in texts]
    print(json.dumps({'verified_in_native_cache':len(found),
                      'server_ids':[m['server_message_id'] for m in found]}),flush=True)
    assert receipt['status']=='sent' and receipt['sent_parts']==2 and len(found)==2
