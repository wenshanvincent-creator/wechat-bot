"""Combine an exact verified UI draft with a new native server-message receipt."""
import json
import os
import re
import time
from pathlib import Path

from sticker_catalog import resolve


def new_image(reader,before,group,self_id):
    found=[m for m in reader.read_messages(group,limit=20)['messages']
           if m['message_id'] not in before and m['sender_id']==self_id
           and m['native_type']==3 and m['server_message_id']]
    return found[0] if len(found)==1 else None


def send_sticker(request,run_ui,self_id):
    from native_reader import NativeReader
    item,_=resolve(request['sticker'])
    operation=request['operation_id']
    if not re.fullmatch(r'[a-z0-9-]{8,64}',operation):
        raise ValueError('Invalid image operation ID')
    root=Path('/state/image-receipts');root.mkdir(mode=0o700,exist_ok=True)
    path=root/(operation+'.json')
    if path.exists():
        receipt=json.loads(path.read_text())
        if receipt['recipient_id']!=request['recipient_id'] or receipt['image_sha256']!=item['sha256']:
            raise ValueError('Image operation belongs to another recipient/payload')
        return receipt
    reader=NativeReader(private_ids=(request['recipient_id'],) if not request['recipient_id'].endswith('@chatroom') else ())
    if reader.account_ref!=request['account_ref']:
        raise ValueError('Image account changed')
    before={m['message_id'] for m in reader.read_messages(request['recipient_id'],limit=20)['messages']}
    receipt=run_ui('/bot/group_image_sender.py',request)
    if (receipt.get('status')=='submitted' and receipt.get('image_draft_matches')
        and receipt.get('return_pressed')):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            message=new_image(reader,before,request['recipient_id'],self_id)
            if message:
                receipt={**receipt,'status':'sent','server_message_id':message['server_message_id'],
                         'message_id':message['message_id'],'sent_at':message['sent_at']}
                break
            time.sleep(.3)
        else:
            receipt={**receipt,'status':'uncertain','error':'No unique new native image receipt; do not resend'}
    receipt={**receipt,'recipient_id':request['recipient_id'],'sticker':item['name'],'image_sha256':item['sha256']}
    temporary=path.with_suffix('.tmp')
    with temporary.open('x') as output:
        json.dump(receipt,output);output.flush();os.fsync(output.fileno())
    temporary.replace(path)
    return receipt
