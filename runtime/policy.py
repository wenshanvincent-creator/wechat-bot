"""Durable input/delivery deduplication. No posting quotas or cooldowns."""
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, group_id TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, day TEXT NOT NULL, text TEXT NOT NULL);
        ''')

    def can_send(self, group):
        return self.db.execute("SELECT 1 FROM decisions WHERE group_id=? AND state IN ('claimed','uncertain')", (group,)).fetchone() is None

    def seen(self, operation):
        return self.db.execute('SELECT 1 FROM decisions WHERE id=?', (operation,)).fetchone() is not None

    def record(self, operation, group, state, now, text, config):
        day = datetime.fromtimestamp(now, ZoneInfo(config['timezone'])).date().isoformat()
        with self.db:
            self.db.execute('INSERT INTO decisions VALUES (?,?,?,?,?,?)', (operation, group, state, now, day, text))

    def finish(self, operation, state):
        with self.db:
            self.db.execute('UPDATE decisions SET state=? WHERE id=?', (state, operation))

    def claim(self, operation, now, text):
        with self.db:
            self.db.execute("UPDATE decisions SET state='claimed', created=?, text=? WHERE id=?", (now, text, operation))


def fresh_inbound(messages, self_id, now, age):
    return [m for m in messages if m.get('text') and m.get('sender_id')
            and m['sender_id'] != self_id
            and 0 <= now - datetime.fromisoformat(m['sent_at']).timestamp() <= age]
