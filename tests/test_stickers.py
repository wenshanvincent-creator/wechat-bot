import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
import deepseek
from sticker_catalog import catalog,resolve
from reply_delivery import transport_parts,deliver
from policy import Ledger
from image_delivery import new_image



@pytest.fixture(autouse=True)
def synthetic_stickers(tmp_path, monkeypatch):
    import hashlib
    import sticker_catalog
    from PIL import Image
    root = tmp_path / 'stickers'
    root.mkdir()
    entries = []
    for index, (name, meaning) in enumerate((('爆笑_真的绷不住了', '大笑'), ('点赞_这波确实厉害', '赞同'))):
        path = root / f'{index}.png'
        Image.new('RGB', (8, 8), (index * 100, 60, 180)).save(path)
        entries.append(dict(name=name, meaning=meaning, file=path.name,
                            sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    (root / 'catalog.json').write_text(json.dumps(entries, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(sticker_catalog, 'ROOT', root)
    monkeypatch.setattr(sticker_catalog, 'DYNAMIC', tmp_path / 'dynamic')


def test_reviewed_assets_have_meaningful_names_and_unknown_paths_are_rejected():
    entries=catalog()
    assert len(entries)==2 and len({item['name'] for item in entries})==2
    item,path=resolve('爆笑_真的绷不住了')
    assert path.suffix=='.png' and '大笑' in item['meaning']
    with pytest.raises(ValueError,match='Unknown'):
        resolve('../private/secrets.env')


@pytest.mark.parametrize('name,valid',[('点赞_这波确实厉害',True),('不存在的表情',False)])
def test_model_selects_only_named_catalog_images(monkeypatch,name,valid):
    reply={'messages':['确实好笑',{'sticker':name}]}
    def open_url(request,timeout):
        body=json.loads(request.data)
        assert '可用表情' in body['messages'][0]['content']
        assert '/bot/' not in body['messages'][0]['content']
        return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps(reply)}}]}).encode())
    monkeypatch.setattr(deepseek.urllib.request,'urlopen',open_url)
    if valid:
        assert deepseek.decide({'model':'deepseek-flash'},[],'','not-a-key')==reply['messages']
    else:
        with pytest.raises(ValueError):
            deepseek.decide({'model':'deepseek-flash'},[],'','not-a-key')


def test_mixed_text_and_images_preserve_order_and_get_distinct_receipts(tmp_path):
    parts=['先说一句',{'sticker':'爆笑_真的绷不住了'},'再说一句']
    assert transport_parts(parts)==parts
    ledger=Ledger(tmp_path/'decisions.sqlite')
    ledger.record('mixed','group','checking',0,'',{'timezone':'UTC'})
    sent=[]
    def sender(group,content,operation,account,deadline):
        sent.append((content,operation));return {'status':'sent'}
    assert deliver(ledger,'group','mixed',parts,'account',sender)['sent_parts']==3
    assert [item[0] for item in sent]==parts
    assert len({item[1] for item in sent})==3


def test_image_receipt_requires_one_fresh_self_image_with_server_ack():
    def message(mid,sender='bot',kind=3,server='123'):
        return dict(message_id=mid,sender_id=sender,native_type=kind,server_message_id=server)
    class Reader:
        messages=[]
        def read_messages(self,group,limit):return {'messages':self.messages}
    reader=Reader();reader.messages=[message('old'),message('other','friend'),message('text',kind=1),message('local',server=None)]
    assert new_image(reader,{'old'},'group','bot') is None
    reader.messages.append(message('new'))
    assert new_image(reader,{'old'},'group','bot')['message_id']=='new'
    reader.messages.append(message('ambiguous'))
    assert new_image(reader,{'old'},'group','bot') is None
