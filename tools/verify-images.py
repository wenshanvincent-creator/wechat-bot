"""Verify loaded image identities; this never starts a container."""
import json
import subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
for item in json.loads((root/'images'/'manifest.json').read_text(encoding='utf-8'))['images']:
    actual=json.loads(subprocess.check_output(['docker','image','inspect',item['image_ref']]))[0]
    assert actual['Id']==item['id'],'Wrong image ID: '+item['image_ref']
    assert actual['Architecture']=='amd64' and actual['Os']=='linux'
print('Verified all three loaded Linux/amd64 image IDs')
