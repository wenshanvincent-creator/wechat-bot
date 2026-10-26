"""Only locally reviewed, named assets may be selected by the model."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent/'stickers'
DYNAMIC=Path('/state/media/stickers')


def catalog():
    result=json.loads((ROOT/'catalog.json').read_text(encoding='utf-8'))
    if (DYNAMIC/'catalog.json').exists():result.extend({**s,'dynamic':True} for s in json.loads((DYNAMIC/'catalog.json').read_text(encoding='utf-8')))
    return result


def resolve(name):
    item=next((s for s in catalog() if s['name']==name),None)
    if item is None:
        raise ValueError('Unknown sticker name')
    root=DYNAMIC if item.get('dynamic') else ROOT
    path=root/item['file']
    if path.parent.resolve()!=root.resolve() or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
        raise ValueError('Sticker asset integrity check failed')
    return item,path


def model_catalog():
    return '\n'.join(s['name']+'：'+s['meaning'] for s in catalog())
