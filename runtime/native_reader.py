"""Read-only native group directory and bounded message pages.

This module never opens chats or changes WeChat's unread state. It exposes
local cached data, not an assurance that the server has synchronized history.
"""
import base64
import hashlib
import json
import re
from datetime import datetime, timezone

from native_keys import PROFILE, discover, process_identity, scan, current_account_root, account_reference, NativeAccountUnavailable
from native_text import decode_text
from native_media import metadata as media_metadata
from wal_snapshot import connect


def _pack(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode()


def _unpack(value):
    if len(value) > 8192:
        raise ValueError('Cursor exceeds size limit')
    try:
        decoded = json.loads(base64.b64decode(value, altchars=b'-_', validate=True))
    except (ValueError, UnicodeError) as error:
        raise ValueError('Invalid native read cursor') from error
    if not isinstance(decoded, dict):
        raise ValueError('Invalid native read cursor')
    return decoded


class NativeReader:
    def __init__(self, private_ids=(), expected_account=None):
        self.private_ids=frozenset(private_ids)
        self.identity = process_identity()
        try:
            self.databases = discover(self.identity)
        except NativeAccountUnavailable:
            if not expected_account:raise
            from native_recovery import wake_bound_account
            wake_bound_account(expected_account,self.identity)
            self.databases = discover(self.identity)
        roots = {item['path'].parent.parent for item in self.databases}
        if len(roots) != 1:
            raise RuntimeError('Database account root is ambiguous')
        self.account_ref = account_reference(next(iter(roots)))
        if expected_account and self.account_ref!=expected_account:
            raise RuntimeError('登录账号与绑定账号不同')
        self.keys, self.scan_stats = scan(self.databases, self.identity)
        if len(self.keys) != len(self.databases):
            raise RuntimeError('Some native database keys were not authenticated')


    def _open(self, item):
        if process_identity() != self.identity:
            raise RuntimeError('WeChat process changed; create a new reader')
        return connect(item['path'], self.keys[item['file_ref']])

    def _database(self, name):
        matches = [item for item in self.databases if item['path'].name == name]
        if len(matches) != 1:
            raise RuntimeError('Required native database is absent or ambiguous')
        return matches[0]

    def list_groups(self):
        item = self._database('contact.db')
        connection, contact_snapshot = self._open(item)
        try:
            groups = [dict(row) for row in connection.execute('''
                SELECT c.username AS conversation_id, c.nick_name AS name,
                       c.remark AS remark, c.is_in_chat_room AS native_membership_flag,
                       c.delete_flag AS native_delete_flag,
                       CASE WHEN r.username IS NULL THEN 0 ELSE 1 END AS room_record_present
                FROM contact c LEFT JOIN chat_room r ON r.username=c.username
                WHERE c.username LIKE '%@chatroom' ORDER BY c.username''')]
        finally:
            connection.close()
        item = self._database('session.db')
        connection, session_snapshot = self._open(item)
        try:
            sessions = {row['username']: dict(row) for row in connection.execute('''
                SELECT username,unread_count,unread_first_msg_srv_id,last_timestamp,is_hidden
                FROM SessionTable WHERE username LIKE '%@chatroom' ''')}
        finally:
            connection.close()
        for group in groups:
            session = sessions.get(group['conversation_id'])
            group['native_session'] = None if session is None else {
                'unread_count_raw': session['unread_count'],
                'first_unread_server_id_raw': str(session['unread_first_msg_srv_id']),
                'last_timestamp_raw': session['last_timestamp'],
                'is_hidden_raw': session['is_hidden'],
                'unread_semantics': 'not_yet_validated_against_client'}
        return {'account_ref': self.account_ref, 'groups': groups,
                'coverage': 'locally cached contact groups; membership flags are raw native fields',
                'snapshots': {'contact': contact_snapshot, 'session': session_snapshot},
                'cross_database_atomic': False, 'wechat_read_state_modified': False}

    def list_friends(self):
        connection,_=self._open(self._database('contact.db'))
        try:
            return [dict(row) for row in connection.execute('''SELECT username AS conversation_id,
                nick_name AS name,remark FROM contact WHERE username NOT LIKE '%@chatroom'
                AND username NOT IN ('filehelper','fmessage','medianote','weixin','newsapp')
                AND (flag & 1)=1 AND delete_flag=0 AND verify_flag=0 ORDER BY nick_name,username''')]
        finally:connection.close()

    def _authorize(self,conversation_id):
        if conversation_id=='filehelper':return
        if conversation_id.endswith('@chatroom') and conversation_id in {g['conversation_id'] for g in self.list_groups()['groups']}:return
        if conversation_id in self.private_ids and conversation_id in {f['conversation_id'] for f in self.list_friends()}:return
        raise ValueError('Conversation is not an explicitly authorized native group/friend')

    def read_messages(self, conversation_id, limit=20, cursor=None):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError('Page limit must be between 1 and 100')
        self._authorize(conversation_id)
        marker = _unpack(cursor) if cursor else None
        if marker and (marker.get('v') != 1 or marker.get('account') != self.account_ref or marker.get('conversation') != conversation_id):
            raise ValueError('Cursor belongs to a different data source or conversation')
        if marker:
            boundary = marker.get('before')
            if not isinstance(boundary, list) or len(boundary) != 4 or any(type(boundary[index]) is not int for index in (0, 1, 3)) or not isinstance(boundary[2], str):
                raise ValueError('Invalid message page cursor boundary')
        table = 'Msg_' + hashlib.md5(conversation_id.encode()).hexdigest()
        results, sources, generations = [], [], {}
        for item in self.databases:
            if not re.fullmatch(r'message_\d+\.db', item['path'].name):
                continue
            connection, metadata = self._open(item)
            try:
                generation_rows = connection.execute('SELECT timestamp FROM TimeStamp').fetchall()
                if len(generation_rows) != 1:
                    raise RuntimeError('Message shard generation is ambiguous')
                generation = str(generation_rows[0][0])
                reference = item['file_ref']
                generations[reference] = generation
                source = {'file_ref': reference, 'generation': generation, 'snapshot': metadata}
                sources.append(source)
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    source['conversation_present'] = False
                    continue
                source['conversation_present'] = True
                if not connection.execute('SELECT 1 FROM Name2Id WHERE user_name=?', (conversation_id,)).fetchone():
                    raise RuntimeError('Message table has no matching native conversation mapping')
                parameters = []
                where = ''
                if marker:
                    where = 'WHERE (create_time,sort_seq,?,local_id) < (?,?,?,?)'
                    parameters = [reference] + marker['before']
                parameters.append(limit + 1)
                rows = connection.execute(f'''SELECT local_id,server_id,local_type,sort_seq,real_sender_id,
                    create_time,message_content,WCDB_CT_message_content
                    FROM "{table}" {where} ORDER BY create_time DESC,sort_seq DESC,local_id DESC LIMIT ?''', parameters).fetchall()
                for row in rows:
                    results.append(self._message(connection, row, conversation_id, reference, generation))
            finally:
                connection.close()
        if marker and marker['generations'] != generations:
            raise ValueError('Message shard set or generation changed; restart pagination')
        results.sort(key=lambda row: row['_order'], reverse=True)
        has_more = len(results) > limit
        page = results[:limit]
        next_cursor = _pack({'v': 1, 'account': self.account_ref, 'conversation': conversation_id,
                             'generations': generations, 'before': page[-1]['_order']}) if has_more else None
        for row in page:
            del row['_order']
        return {'account_ref': self.account_ref, 'conversation_id': conversation_id, 'messages': page,
                'next_cursor': next_cursor, 'has_more_local': has_more,
                'order': 'descending create_time, sort_seq, shard reference, local_id',
                'coverage': 'currently local cached message tables; not full synchronized history',
                'cross_database_atomic': False, 'sources': sources, 'wechat_read_state_modified': False}

    def _message(self, connection, row, conversation_id, reference, generation):
        sender = connection.execute('SELECT user_name FROM Name2Id WHERE rowid=?', (row['real_sender_id'],)).fetchone()
        body = row['message_content']
        flag = row['WCDB_CT_message_content']
        if row['local_type'] == 1:
            text, content_status = decode_text(body, flag)
        else:
            text, content_status = None, 'unsupported_native_message_type'
        media=None
        if row['local_type'] in (3,47):
            decoded,_=decode_text(body,flag)
            media=media_metadata(decoded,row['local_type'])
        message = {'conversation_id': conversation_id,
            'message_id': f"server:{self.account_ref}:{hashlib.md5(conversation_id.encode()).hexdigest()}:{row['server_id']}" if row['server_id'] else f"local:{reference}:{generation}:{row['local_id']}",
            'id_source': 'native server_id scoped to account/conversation' if row['server_id'] else 'native local_id scoped to shard generation',
            'server_message_id': str(row['server_id']) if row['server_id'] else None,
            'sender_id': sender[0] if sender else None,
            'sender_mapping': 'native shard Name2Id' if sender else 'unresolved',
            'sent_at': datetime.fromtimestamp(row['create_time'], timezone.utc).isoformat(),
            'native_type': row['local_type'], 'text': text, 'content_status': content_status,
            'native_compression_flag': flag,
            **({'media':media} if media else {}),
            'source': {'file_ref': reference, 'generation': generation, 'local_id': str(row['local_id']),
                       'native_sender_rowid': str(row['real_sender_id']), 'sort_seq': str(row['sort_seq'])},
            '_order': [row['create_time'], row['sort_seq'], reference, row['local_id']]}
        return message

    def media_details(self,message):
        self._authorize(message['conversation_id'])
        from native_media import details
        item=next(d for d in self.databases if d['file_ref']==message['source']['file_ref'])
        connection,_=self._open(item)
        try:
            table='Msg_'+hashlib.md5(message['conversation_id'].encode()).hexdigest()
            row=connection.execute(f'SELECT message_content,WCDB_CT_message_content,local_type,server_id FROM "{table}" WHERE local_id=?',(message['source']['local_id'],)).fetchone()
            if not row or str(row['server_id'])!=str(message['server_message_id'] or 0):raise ValueError('图片来源行已改变')
            text,_=decode_text(row['message_content'],row['WCDB_CT_message_content'])
            return details(text,row['local_type'])
        finally:connection.close()

    def insertions(self, conversation_id, position=None, limit=20, from_now=False):
        """Incremental local inserts. Existing-row edits/deletions are not events.

        Each shard advances by local_id, not by timestamp: an older message
        synchronized later must not disappear behind a wall-clock watermark.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError('Page limit must be between 1 and 100')
        self._authorize(conversation_id)
        if position and (position['account'] != self.account_ref or position['conversation'] != conversation_id or position['v'] != 1):
            raise ValueError('Incremental position belongs to another source')
        if from_now and position is not None:
            raise ValueError('A baseline cannot overwrite an existing incremental position')
        table = 'Msg_' + hashlib.md5(conversation_id.encode()).hexdigest()
        shards, messages, sources = {}, [], []
        has_more = False
        for item in self.databases:
            if not re.fullmatch(r'message_\d+\.db', item['path'].name):
                continue
            reference = item['file_ref']
            connection, metadata = self._open(item)
            try:
                generation_rows = connection.execute('SELECT timestamp FROM TimeStamp').fetchall()
                if len(generation_rows) != 1:
                    raise RuntimeError('Message shard generation is ambiguous')
                generation = str(generation_rows[0][0])
                prior = position['shards'].get(reference) if position else None
                if position and (prior is None or prior['generation'] != generation):
                    raise ValueError('Message shard generation changed; explicit resync required')
                after = prior['after_local_id'] if prior else 0
                present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
                if present:
                    if not connection.execute('SELECT 1 FROM Name2Id WHERE user_name=?', (conversation_id,)).fetchone():
                        raise RuntimeError('Message table has no native conversation mapping')
                    if from_now:
                        after = connection.execute(f'SELECT coalesce(max(local_id),0) FROM "{table}"').fetchone()[0]
                    else:
                        remaining = limit - len(messages)
                        rows = connection.execute(f'''SELECT local_id,server_id,local_type,sort_seq,real_sender_id,
                            create_time,message_content,WCDB_CT_message_content FROM "{table}"
                            WHERE local_id>? ORDER BY local_id LIMIT ?''', (after, remaining + 1)).fetchall()
                        has_more = has_more or len(rows) > remaining
                        for row in rows[:remaining]:
                            message = self._message(connection, row, conversation_id, reference, generation)
                            del message['_order']
                            messages.append(message)
                            after = row['local_id']
                shards[reference] = {'generation': generation, 'after_local_id': after}
                sources.append({'file_ref': reference, 'snapshot': metadata, 'conversation_present': present})
            finally:
                connection.close()
        if position and set(position['shards']) != set(shards):
            raise ValueError('Message shard set changed; explicit resync required')
        return {'messages': messages, 'position': {'v': 1, 'account': self.account_ref,
                'conversation': conversation_id, 'shards': shards}, 'has_more_local': has_more,
                'sources': sources, 'coverage': 'new local rows only; includes later backfill; edits/deletes are not tracked',
                'order': 'shard reference, local_id ascending', 'wechat_read_state_modified': False}

    def read_unread(self, conversation_id, limit=20, cursor=None):
        """Resolve this client's native unread index without marking anything read.

        An index revision pins pagination. Changed native unread membership
        requires a new read instead of silently mixing two different lists.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError('Page limit must be between 1 and 100')
        if conversation_id not in {group['conversation_id'] for group in self.list_groups()['groups']}:
            raise ValueError('Native unread reads require a group from the native directory')
        connection, session_snapshot = self._open(self._database('session.db'))
        try:
            session = connection.execute('SELECT unread_count,unread_first_msg_srv_id FROM SessionTable WHERE username=?', (conversation_id,)).fetchone()
            first_pointer = session['unread_first_msg_srv_id'] if session else None
            entries = [dict(row) for row in connection.execute('''
                SELECT u.server_id,u.local_id,u.create_time FROM SessionUnreadListTable_1 u
                JOIN Name2Id n ON n.rowid=u.username_id WHERE n.user_name=?
                ORDER BY CASE WHEN u.server_id=? THEN 0 ELSE 1 END,u.create_time,u.server_id''', (conversation_id, first_pointer))]
        finally:
            connection.close()
        count = session['unread_count'] if session else None
        first = str(session['unread_first_msg_srv_id']) if session and session['unread_first_msg_srv_id'] else None
        revision = hashlib.sha256(json.dumps([count, first, entries], sort_keys=True).encode()).hexdigest()
        marker = _unpack(cursor) if cursor else None
        if marker and (marker.get('kind') != 'native_unread' or marker.get('account') != self.account_ref or marker.get('conversation') != conversation_id or marker.get('revision') != revision):
            raise ValueError('Native unread index or cursor source changed; restart this read')
        offset = marker['offset'] if marker else 0
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= len(entries):
            raise ValueError('Invalid native unread cursor offset')
        selected = entries[offset:offset + limit]
        wanted = [entry['server_id'] for entry in selected]
        candidates, sources = {}, []
        table = 'Msg_' + hashlib.md5(conversation_id.encode()).hexdigest()
        for item in self.databases:
            if not re.fullmatch(r'message_\d+\.db', item['path'].name) or not wanted:
                continue
            connection, metadata = self._open(item)
            try:
                sources.append({'file_ref': item['file_ref'], 'snapshot': metadata})
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                generation_rows = connection.execute('SELECT timestamp FROM TimeStamp').fetchall()
                if len(generation_rows) != 1:
                    raise RuntimeError('Message shard generation is ambiguous')
                generation = str(generation_rows[0][0])
                for row in connection.execute(f'''SELECT local_id,server_id,local_type,sort_seq,real_sender_id,
                    create_time,message_content,WCDB_CT_message_content FROM "{table}"
                    WHERE server_id IN ({','.join('?' for _ in wanted)})''', wanted):
                    message = self._message(connection, row, conversation_id, item['file_ref'], generation)
                    del message['_order']
                    candidates.setdefault(str(row['server_id']), []).append(message)
            finally:
                connection.close()
        messages, missing, ambiguous = [], [], []
        for entry in selected:
            identity = str(entry['server_id'])
            found = candidates.get(identity, [])
            if len(found) == 1:
                messages.append(found[0])
            elif not found:
                missing.append(identity)
            else:
                ambiguous.append(identity)
        next_offset = offset + len(selected)
        next_cursor = _pack({'kind': 'native_unread', 'account': self.account_ref, 'conversation': conversation_id,
                             'revision': revision, 'offset': next_offset}) if next_offset < len(entries) else None
        return {'account_ref': self.account_ref, 'conversation_id': conversation_id,
                'native_unread_count': count, 'native_first_unread_server_id': first,
                'first_pointer_present_in_index': any(str(entry['server_id']) == first for entry in entries) if first else False,
                'indexed_unread_count': len(entries), 'index_matches_counter': count == len(entries) if count is not None else None,
                'messages': messages, 'missing_server_ids': missing, 'ambiguous_server_ids': ambiguous,
                'next_cursor': next_cursor, 'index_revision': revision, 'session_snapshot': session_snapshot,
                'sources': sources, 'cross_database_atomic': False,
                'validation': 'native client index; internal relations checked; UI display not yet compared',
                'coverage': 'this server client native unread index; missing local rows explicitly listed',
                'order': 'native first-unread pointer first; remaining index create_time/server_id ascending',
                'wechat_read_state_modified': False, 'consumer_progress_modified': False}

    def assert_active_account(self):
        if account_reference(current_account_root(self.identity))!=self.account_ref:
            raise RuntimeError('登录账号与绑定账号不同')


