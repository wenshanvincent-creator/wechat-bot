"""A group participant with named reactions, no model shell/account tools."""
import hashlib
import json
import os
import signal
import subprocess
import time
import urllib.error
from pathlib import Path

from deepseek import decide
from consumer_state import ConsumerState
from native_reader import NativeReader
from policy import Ledger, fresh_inbound
from group_memory import GroupMemory
from conversation_context import ConversationContext, TokenCounter
from model_view import GroupView
from reply_delivery import deliver

STATE = Path('/state')
CONFIG = Path('/bot-config')
STOPPING = False


def stop(*_):
    global STOPPING
    STOPPING = True


def run_ui(script,request):
    process = subprocess.run(
        ['/usr/bin/setpriv', '--reuid=1000', '--regid=1000', '--init-groups',
         '/lsiopy/bin/python3', script],
        input=json.dumps(request), capture_output=True, text=True, timeout=150 if script.endswith('media_ui.py') else 110 if script.endswith('moments_ui.py') else 75,
        env={**os.environ, 'HOME': '/config', 'DISPLAY': ':1'})
    if process.returncode:
        detail=process.stderr.strip().splitlines()[-1] if process.stderr.strip() else 'UI process exited without a receipt'
        raise RuntimeError('UI 操作失败：'+detail[:300])
    return json.loads(process.stdout)


def read_settled(reader, group_id, limit=20, attempts=6):
    """Read the native cache without treating a concurrent write as a failure.

    WeChat keeps writing its WAL while we read, which makes the snapshot guard
    throw SnapshotChanged; that is a race, not a send outcome.
    """
    from wal_snapshot import SnapshotChanged
    last=None
    for attempt in range(attempts):
        try:
            return reader.read_messages(group_id, limit=limit)['messages']
        except SnapshotChanged as error:
            last=error
            time.sleep(0.2+0.15*attempt)
    raise last


def send(group_id, content, operation, account_ref, deadline,self_id=None):
    if self_id is None:self_id=json.loads((CONFIG/'config.json').read_text())['self_sender_id']
    request=dict(action='send',recipient_id=group_id,operation_id=operation,
                 account_ref=account_ref,not_after_ms=int(deadline*1000))
    if isinstance(content,dict):
        from image_delivery import send_sticker
        return send_sticker({**request,'sticker':content['sticker']},run_ui,self_id)
    # The client can add menu rows dynamically. If visual copy verification
    # fails after Return, reconcile the exact fresh outgoing text from native
    # server receipts rather than blocking an already delivered reply.
    reader=NativeReader(private_ids=(group_id,) if not group_id.endswith('@chatroom') else ())
    if reader.account_ref!=account_ref:
        raise ValueError('Text account changed')
    before={m['message_id'] for m in read_settled(reader,group_id)}
    receipt=run_ui('/bot/group_sender.py',{**request,'text':content})
    if receipt.get('status') in ('sent','uncertain'):
        end=time.monotonic()+8
        while time.monotonic()<end:
            try:
                found=[m for m in read_settled(reader,group_id,attempts=2)
                       if m['message_id'] not in before and m['sender_id']==self_id
                       and m['text']==content and m['server_message_id']]
            except Exception:
                found=[]
            if len(found)==1:
                receipt={**receipt,'status':'sent','reconciled_from_native_cache':True,
                         'server_message_id':found[0]['server_message_id'],'message_id':found[0]['message_id'],'sent_at':found[0]['sent_at']};break
            time.sleep(.3)
        else:receipt={**receipt,'status':'uncertain','error':'没有唯一的新服务端文字回执，请核对聊天，不自动重发'}
    return receipt


def main():
    os.umask(0o077)
    config = json.loads((CONFIG / 'config.json').read_text())
    if not config['enabled'] or not config['groups'] or not config['account_ref'] or not config['self_sender_id']:
        raise RuntimeError('Bot requires an enabled config, account identity and explicit groups')
    secrets = dict(line.split('=', 1) for line in (CONFIG / 'secrets.env').read_text().splitlines() if '=' in line)
    key = secrets.get('DEEPSEEK_API_KEY', '').strip()
    if not key:
        raise RuntimeError('DEEPSEEK_API_KEY is missing')
    STATE.mkdir(exist_ok=True)
    progress = ConsumerState(STATE / 'consumer.sqlite')
    ledger = Ledger(STATE / 'decisions.sqlite')
    memory = GroupMemory(STATE / 'group-memory.sqlite', config['account_ref'])
    conversation = ConversationContext(STATE / 'context.sqlite', config['account_ref'], TokenCounter())
    reader = NativeReader()
    if reader.account_ref != config['account_ref']:
        raise RuntimeError('Logged-in account differs from the enrolled bot account')
    views = {}
    for group in config['groups']:
        views[group] = GroupView.from_reader(reader,group,config['self_sender_id'],config['timezone'])
        if not conversation.initialized(group):
            recent = reader.read_messages(group, limit=20)['messages']
            conversation.append(group, list(reversed(recent)))
        progress.start(reader, group, 'deepseek-bot', 'from_now')
    print(json.dumps({'status': 'ready', 'groups': config['groups'],
                      'reply_policy': 'model_decides', 'multipart': True, 'stickers': True,
                      'memory': 'langmem-sqlite',
                      'context_compress_at': config['context_window_tokens'] * config['context_compress_ratio']}), flush=True)
    while not STOPPING and not (STATE / 'STOP').exists():
        for group in config['groups']:
            batch = progress.poll(reader, group, 'deepseek-bot', limit=50)
            if not batch['batch_id']:
                continue
            operation = hashlib.sha256((config['account_ref'] + group + batch['batch_id']).encode()).hexdigest()
            now = time.time()
            fresh = fresh_inbound(batch['messages'], config['self_sender_id'], now, config['max_event_age_seconds'])
            conversation.append(group, batch['messages'])
            memory.enqueue(group, fresh)
            personality = (CONFIG / 'personality.md').read_text()
            view = views[group] = GroupView.from_reader(reader,group,config['self_sender_id'],config['timezone'])
            memories = view.memories(memory.recall(group, fresh))
            context = None
            try:
                context, summary, context_status = conversation.prepare(group, config, personality, memories, key,view=view)
                if context_status['compressed'] or fresh:
                    print(json.dumps({'group': group, 'status': 'context', **context_status}), flush=True)
            except (RuntimeError, ValueError, urllib.error.URLError, TimeoutError) as error:
                # A failed summarization keeps all original context. Skip this
                # reply and try again on a later event; memory learning continues.
                print(json.dumps({'group': group, 'status': 'context_error',
                                  'error_type': type(error).__name__}), flush=True)
            if not ledger.seen(operation):
                # Reserve this input before contacting the API; restart never repeats
                # a decision request whose response or delivery may be uncertain.
                ledger.record(operation, group, 'checking', now, '', config)
                messages = []
                if fresh and context is not None and ledger.can_send(group):
                    messages = decide(config, context, personality, key, memories, summary,
                        on_usage=lambda body, usage: conversation.record_usage(group, body, usage))
                if (messages and not STOPPING and not (STATE / 'STOP').exists()
                    and fresh_inbound(fresh, config['self_sender_id'], time.time(), config['max_event_age_seconds'])):
                    receipt = deliver(ledger,group,operation,messages,config['account_ref'],send,
                        stopping=lambda:STOPPING or (STATE/'STOP').exists())
                    # Native image rows have no decoded text. Keep their semantic
                    # names in context so the model remembers what it just sent.
                    for row in ledger.db.execute('SELECT receipt FROM reply_parts WHERE operation=? ORDER BY part',(operation,)):
                        part=json.loads(row[0])
                        if part.get('status')=='sent' and part.get('sticker'):
                            conversation.append(group,[dict(message_id=part['message_id'],sender_id=config['self_sender_id'],
                                sent_at=part['sent_at'],text='[表情：'+part['sticker']+']')])
                    print(json.dumps({'group': group, 'operation': operation, **receipt}), flush=True)
                else:
                    ledger.finish(operation, 'silent')
                    print(json.dumps({'group': group, 'operation': operation,
                                      'status': 'silent', 'fresh_inbound': len(fresh)}), flush=True)
            progress.acknowledge(reader, group, 'deepseek-bot', batch['batch_id'])
        # Learning still runs while the model chooses silence.
        # A failed memory API request preserves its queue and is retried after five minutes.
        for group in config['groups']:
            if STOPPING or (STATE / 'STOP').exists():
                return
            try:
                result = memory.consolidate(group, config, key, time.time(),view=views[group])
                if result is not None:
                    print(json.dumps({'group': group, 'status': 'memory_updated', **result}), flush=True)
            except Exception as error:
                print(json.dumps({'group': group, 'status': 'memory_error',
                                  'error_type': type(error).__name__}), flush=True)
        for _ in range(config['poll_seconds']):
            if STOPPING or (STATE / 'STOP').exists():
                return
            time.sleep(1)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    main()
