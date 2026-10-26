"""Private consumer progress, separate from WeChat's native unread state.

Reads replay the same pending batch until acknowledged. Acknowledgement and
ID deduplication commit together; loss of a response cannot silently skip data.
"""
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path


class ConsumerState:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS consumer(
                scope TEXT PRIMARY KEY, mode TEXT NOT NULL, position TEXT,
                pending TEXT, last_ack TEXT);
            CREATE TABLE IF NOT EXISTS seen(
                scope TEXT NOT NULL, message_id TEXT NOT NULL,
                PRIMARY KEY(scope,message_id));
        ''')

    def close(self):
        self.connection.close()

    def _scope(self, reader, conversation_id, consumer_id):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', consumer_id):
            raise ValueError('Consumer ID must contain 1-64 letters, digits, underscores or hyphens')
        return json.dumps([reader.account_ref, conversation_id, consumer_id], separators=(',', ':'))

    def start(self, reader, conversation_id, consumer_id, mode):
        if mode not in ('from_now', 'existing_local'):
            raise ValueError('Choose from_now or existing_local explicitly')
        scope = self._scope(reader, conversation_id, consumer_id)
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            prior = self.connection.execute('SELECT * FROM consumer WHERE scope=?', (scope,)).fetchone()
            if prior:
                if prior['mode'] != mode:
                    raise ValueError('Consumer already exists with another start mode; no progress reset')
                return {'created': False, 'mode': mode, 'pending': prior['pending'] is not None}
            baseline = reader.insertions(conversation_id, from_now=True)['position']
            # A baseline taken before WeChat has materialized any message shard
            # carries no progress and would later fail every poll, so defer it.
            if not baseline['shards']:
                raise ValueError('微信消息库尚未就绪，稍后重试')
            position = baseline
            if mode == 'existing_local':
                for shard in position['shards'].values():
                    shard['after_local_id'] = 0
            self.connection.execute('INSERT INTO consumer(scope,mode,position) VALUES (?,?,?)',
                                    (scope, mode, json.dumps(position)))
            return {'created': True, 'mode': mode, 'pending': False,
                    'wechat_read_state_modified': False}

    def poll(self, reader, conversation_id, consumer_id, limit=20):
        scope = self._scope(reader, conversation_id, consumer_id)
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            state = self.connection.execute('SELECT * FROM consumer WHERE scope=?', (scope,)).fetchone()
            if state is None:
                raise ValueError('Consumer is not initialized')
            if state['pending'] is not None:
                return dict(json.loads(state['pending']), replayed=True)
            position = json.loads(state['position']) if state['position'] else None
            if position is not None and not position.get('shards'):
                # Self-heal a baseline stored before the message shards existed.
                fresh = reader.insertions(conversation_id, from_now=True)['position']
                if fresh.get('shards'):
                    self.connection.execute('UPDATE consumer SET position=? WHERE scope=?', (json.dumps(fresh), scope))
                    position = fresh
            result = reader.insertions(conversation_id, position=position, limit=limit)
            messages = []
            batch_ids = set()
            for message in result['messages']:
                identity = message['message_id']
                if identity not in batch_ids and not self.connection.execute('SELECT 1 FROM seen WHERE scope=? AND message_id=?', (scope, identity)).fetchone():
                    messages.append(message)
                    batch_ids.add(identity)
            if not messages:
                self.connection.execute('UPDATE consumer SET position=? WHERE scope=?', (json.dumps(result['position']), scope))
                return {'batch_id': None, 'messages': [], 'has_more_local': result['has_more_local'],
                        'replayed': False, 'coverage': result['coverage'], 'sources': result['sources'],
                        'wechat_read_state_modified': False}
            identity = json.dumps([scope, result['position'], [message['message_id'] for message in messages]], sort_keys=True)
            batch = {'batch_id': hashlib.sha256(identity.encode()).hexdigest(), 'messages': messages,
                     'next_position': result['position'], 'has_more_local': result['has_more_local'],
                     'coverage': result['coverage'], 'sources': result['sources'],
                     'wechat_read_state_modified': False}
            # The bridge has a 4 MiB response budget. Do not persist a pending
            # batch that cannot be delivered or reduced on a subsequent poll.
            if len(json.dumps(batch, ensure_ascii=False).encode()) > 3 * 1024 * 1024:
                raise ValueError('Batch exceeds delivery budget; retry with a smaller page limit')
            self.connection.execute('UPDATE consumer SET pending=? WHERE scope=?', (json.dumps(batch), scope))
            return dict(batch, replayed=False)

    def acknowledge(self, reader, conversation_id, consumer_id, batch_id):
        if not isinstance(batch_id, str) or not re.fullmatch(r'[0-9a-f]{64}', batch_id):
            raise ValueError('Batch ID must be the issued batch identifier')
        scope = self._scope(reader, conversation_id, consumer_id)
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            state = self.connection.execute('SELECT * FROM consumer WHERE scope=?', (scope,)).fetchone()
            if state is None:
                raise ValueError('Consumer is not initialized')
            if batch_id == state['last_ack']:
                return {'acknowledged': True, 'already_acknowledged': True, 'wechat_read_state_modified': False}
            batch = json.loads(state['pending']) if state['pending'] else None
            if batch is None or batch_id != batch['batch_id']:
                raise ValueError('Batch is not the pending batch; progress unchanged')
            self.connection.executemany('INSERT OR IGNORE INTO seen(scope,message_id) VALUES (?,?)',
                                        [(scope, message['message_id']) for message in batch['messages']])
            self.connection.execute('UPDATE consumer SET position=?,pending=NULL,last_ack=? WHERE scope=?',
                                    (json.dumps(batch['next_position']), batch_id, scope))
            return {'acknowledged': True, 'already_acknowledged': False, 'wechat_read_state_modified': False}
