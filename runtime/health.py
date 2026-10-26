"""Check the new bot profile, never reads message bodies or sends."""
import json
import argparse
from pathlib import Path
from native_keys import active_account_roots
from native_reader import NativeReader

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--group')
    args = parser.parse_args()
    reader = NativeReader()
    groups = reader.list_groups()
    selected = [g for g in groups['groups'] if not args.group or g['name'] == args.group]
    roots = active_account_roots(reader.identity)
    print(json.dumps({'account_ref': reader.account_ref, 'account_folder': roots[0].parent.name,
                     'groups': selected, 'cached_group_count': len(groups['groups'])}, ensure_ascii=False))
