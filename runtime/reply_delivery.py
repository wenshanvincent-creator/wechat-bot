"""Ordered chat bubbles with independent durable transport receipts."""
import hashlib
import json
import re
import time


def humanize(text):
    """Drop sentence-final Chinese periods; people text without them."""
    text = text.replace('。', ' ')
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    return text.strip()


def transport_parts(messages):
    parts = []
    for message in messages:
        if isinstance(message,dict):
            parts.append(message)
            continue
        message = humanize(message)
        while len(message) > 2000:
            cut = max(message.rfind('\n',0,2001),message.rfind(' ',0,2001))
            cut = cut if cut > 0 else 2000
            parts.append(message[:cut].strip())
            message = message[cut:].strip()
        if message:
            parts.append(message)
    return parts


def deliver(ledger, group, operation, messages, account, sender, stopping=lambda:False):
    parts = transport_parts(messages)
    ledger.claim(operation,time.time(),json.dumps(parts,ensure_ascii=False))
    ledger.db.execute('''CREATE TABLE IF NOT EXISTS reply_parts (
        operation TEXT, part INTEGER, receipt TEXT, PRIMARY KEY(operation,part))''')
    ledger.db.commit()
    sent = 0
    for index,text in enumerate(parts):
        if stopping():
            status = 'partial' if sent else 'cancelled'
            ledger.finish(operation,status)
            return {'status':status,'sent_parts':sent,'total_parts':len(parts)}
        child = hashlib.sha256(f'{operation}:{index}'.encode()).hexdigest()
        try:
            receipt = sender(group,text,child,account,time.time()+45)
        except Exception as error:
            message = str(error)
            # Failures raised before the UI touched the chat cannot have sent
            # anything; marking them "failed" avoids blocking the chat forever.
            before_send = (isinstance(error, KeyError) or 'KeyError' in message
                           or 'No unique enrolled' in message
                           or '微信主窗口不唯一' in message
                           or 'Enrolled conversation title' in message
                           or 'Verified window' in message)
            receipt = {'status':'failed' if before_send else 'uncertain',
                       'error_type':type(error).__name__,'error':message[:200]}
        with ledger.db:
            ledger.db.execute('INSERT INTO reply_parts VALUES (?,?,?)',
                              (operation,index,json.dumps(receipt)))
        if receipt.get('status') != 'sent':
            status = ('partial' if sent else 'failed') if receipt.get('status') in ('failed','expired') else 'uncertain'
            ledger.finish(operation,status)
            return {'status':status,'sent_parts':sent,'total_parts':len(parts)}
        sent += 1
    ledger.finish(operation,'sent')
    return {'status':'sent','sent_parts':sent,'total_parts':len(parts)}
