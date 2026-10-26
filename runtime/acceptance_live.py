"""Explicit operator acceptance command. --send really messages one configured chat."""
import argparse
import hashlib
import json
import time
import uuid
from control_store import ControlStore
from agent_state import AgentState,ScopedStores
from conversation_context import TokenCounter
from provider_client import ProviderClient
from native_reader import NativeReader
from model_view import GroupView
from policy import Ledger
from reply_delivery import deliver
from bot import send


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--channel',required=True);parser.add_argument('--send',action='store_true');args=parser.parse_args()
    control=ControlStore('/state');channel=control.get('channels',args.channel)
    if not channel.get('enabled') or not channel.get('allow_read') or not channel.get('allow_send'):raise ValueError('该聊天没有验收所需的读写权限')
    account=control.meta('account');session=control.get('sessions',channel['session_id']);persona=control.get('personas',session['persona_ids'][0]);provider=control.get('providers',persona['provider_id'])
    reader=NativeReader(private_ids=(channel['native_id'],) if channel['kind']=='friend' else ())
    if reader.account_ref!=account['account_ref']:raise ValueError('账号已变化')
    recent=reader.read_messages(channel['native_id'],limit=10)['messages']
    view=GroupView.from_reader(reader,channel['native_id'],account['self_sender_id'])
    client=ProviderClient(provider,control.secret(provider['id']))
    messages,usage=client.decision(persona['persona'],[{'speaker':'控制台验收','text':'你正在进行已授权的本地'+('私聊' if channel['kind']=='friend' else '群聊')+'功能测试。请只发一条简短通知，明确说明是本地功能测试，可请对方回一句“测试通过”。不要宣称对方已回复，不调用工具，不发表情。'}])
    if len(messages)!=1 or not isinstance(messages[0],str):raise ValueError('验收要求一条文字，模型没有返回该格式')
    result={'channel':channel['name'],'inbound_rows_read':len(recent),'resolved_speakers':sorted({view.name(m['sender_id']) for m in recent if m.get('sender_id')}),'model_messages':messages,'usage':usage,'wechat_messages_sent':0}
    if args.send:
        operation=hashlib.sha256(('acceptance:'+uuid.uuid4().hex).encode()).hexdigest();ledger=Ledger('/state/decisions.sqlite')
        if not ledger.can_send(channel['native_id']):raise ValueError('该聊天存在需要核对的发送回执')
        ledger.record(operation,channel['native_id'],'checking',time.time(),'',{'timezone':'Asia/Shanghai'})
        def sender(native,content,child,account_ref,deadline):
            current=control.get('channels',channel['id'])
            if not current.get('enabled') or not current.get('allow_send'):return {'status':'failed','error':'聊天权限已关闭'}
            return send(native,content,child,account_ref,deadline,account['self_sender_id'])
        receipt=deliver(ledger,channel['native_id'],operation,messages,account['account_ref'],sender,stopping=lambda:False)
        conversation=ScopedStores(control,TokenCounter()).conversation(session['id']);turns=AgentState(control)
        for row in ledger.db.execute('SELECT receipt FROM reply_parts WHERE operation=?',(operation,)):
            part=json.loads(row[0])
            if part.get('status')!='sent':continue
            turns.remember_outgoing(part['message_id'],session['id'],persona['id'])
            conversation.append(session['id'],[dict(message_id=part['message_id'],sender_id='agent:'+persona['id'],agent_id=persona['id'],display_name=persona['name'],channel_name=channel['name'],sent_at=part['sent_at'],text=messages[0])])
        conversation.db.close();result.update(receipt=receipt,wechat_messages_sent=receipt['sent_parts'])
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
