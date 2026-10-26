"""Persistent per-group rolling context, compacted at half of the model budget."""
import json
import sqlite3
from pathlib import Path

from deepseek import decision_body, summarize
from model_view import GroupView


class TokenCounter:
    def __init__(self):
        from tokenizers import Tokenizer
        directory = Path('/opt/deepseek-tokenizer')
        self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self.bos = json.loads((directory / 'tokenizer_config.json').read_text())['bos_token']['content']

    def count(self, body):
        # The decision request always has one system and one user message. This
        # renders that subset of the official archive's chat template, with no
        # downloaded Python code, transformers, model weights or inference.
        system, user = body['messages']
        text = self.bos + system['content'] + '<｜User｜>' + user['content'] + '<｜Assistant｜>'
        # Encoding a half-million-token string at once exhausted the worker's
        # 256 MiB limit in the live resource check. Bound tokenizer working
        # memory; token merges at chunk edges make this a close estimate, then
        # API usage calibrates any positive server-side difference.
        return sum(len(self.tokenizer.encode(text[start:start+16384],
                       add_special_tokens=False).ids) for start in range(0,len(text),16384))


class ConversationContext:
    def __init__(self, path, account, counter):
        self.account, self.counter = account, counter
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS turns (
                account TEXT, group_id TEXT, message_id TEXT, payload TEXT,
                PRIMARY KEY(account,group_id,message_id));
            CREATE TABLE IF NOT EXISTS summaries (
                account TEXT, group_id TEXT, summary TEXT NOT NULL DEFAULT '',
                token_overhead INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(account,group_id));
        ''')

    def initialized(self, group):
        return self.db.execute('SELECT 1 FROM summaries WHERE account=? AND group_id=?',
                               (self.account, group)).fetchone() is not None

    def append(self, group, messages):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO summaries(account,group_id) VALUES (?,?)',
                            (self.account,group))
            self.db.executemany('INSERT OR IGNORE INTO turns VALUES (?,?,?,?)', [
                (self.account, group, m['message_id'], json.dumps({
                    'message_id': m['message_id'], 'sender': m['sender_id'],
                    'sent_at': m['sent_at'], 'text': m['text'],
                    **{k:m[k] for k in ('display_name','channel_name','agent_id') if k in m}}, ensure_ascii=False))
                for m in messages if m.get('text')])

    def snapshot(self, group):
        row = self.db.execute('SELECT summary,token_overhead FROM summaries WHERE account=? AND group_id=?',
                              (self.account,group)).fetchone()
        turns = self.db.execute('SELECT message_id,payload FROM turns WHERE account=? AND group_id=? '
            'AND payload IS NOT NULL ORDER BY rowid', (self.account,group)).fetchall()
        return row[0], row[1], [(mid,json.loads(payload)) for mid,payload in turns]

    def record_usage(self, group, body, usage):
        overhead = max(0, usage['prompt_tokens'] - self.counter.count(body))
        with self.db:
            self.db.execute('UPDATE summaries SET token_overhead=? WHERE account=? AND group_id=?',
                            (overhead,self.account,group))

    def prepare(self, group, config, personality, memories, key, compressor=summarize, view=None, channel=None):
        summary, overhead, rows = self.snapshot(group)
        if channel:
            rows = [(mid, message) for mid, message in rows
                    if (message or {}).get('channel_name') == channel]
        view = view or GroupView()
        summary = view.digest(summary)
        messages = view.messages([message for _, message in rows])
        def size(items, digest):
            body = decision_body(config,items,personality,memories,digest)
            return self.counter.count(body) + overhead + body['max_tokens']
        before = size(messages, summary)
        threshold = int(config['context_window_tokens'] * config['context_compress_ratio'])
        limit = int(config.get('max_transcript_messages', 80))
        keep_target = int(config.get('keep_recent_messages', 20))
        if before < threshold and len(rows) <= limit:
            return messages, summary, {'estimated_tokens':before, 'threshold':threshold, 'compressed':False}
        keep = min(keep_target, len(rows)-1)
        if keep < 1:
            raise ValueError('Latest message alone exceeds the compression budget')
        # Preserve 20 originals normally. With unusually large recent messages,
        # move more into the summary but always keep the newest original.
        while keep > 1 and size(messages[-keep:], summary) >= threshold:
            keep -= 1
        prefix = rows[:-keep]
        new_summary = compressor(config, messages[:-keep], summary, key)
        remaining = messages[-keep:]
        after = size(remaining, new_summary)
        if after >= threshold or after >= before:
            raise ValueError('Compression did not reduce context below the threshold; history retained')
        with self.db:
            self.db.execute('UPDATE summaries SET summary=? WHERE account=? AND group_id=?',
                            (new_summary,self.account,group))
            # Retain IDs as deduplication receipts so synchronized old messages
            # cannot be re-added after their payload has been summarized.
            self.db.executemany('UPDATE turns SET payload=NULL WHERE account=? AND group_id=? AND message_id=?',
                               [(self.account,group,mid) for mid,_ in prefix])
        return remaining, new_summary, {'estimated_tokens':after, 'before_tokens':before,
            'threshold':threshold, 'compressed':True, 'summarized_messages':len(prefix), 'kept_messages':keep}
