"""Durable human/agent turn arbitration and physically separated session stores."""
import time
from control_store import active_persona
from conversation_context import ConversationContext
from group_memory import GroupMemory


class AgentState:
    def __init__(self,control):
        self.control=control;self.db=control.db
        columns={r[1] for r in self.db.execute('PRAGMA table_info(session_run)')}
        if 'burst_started' not in columns:
            self.db.execute('ALTER TABLE session_run ADD COLUMN burst_started REAL');self.db.commit()
        self.db.execute('CREATE TABLE IF NOT EXISTS outgoing_events(id TEXT PRIMARY KEY,session_id TEXT,agent_id TEXT,created REAL)');self.db.commit()

    def human(self,session,event_id,channel,now=None,debounce_ms=1500):
        now=time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            inserted=self.db.execute('INSERT OR IGNORE INTO human_events VALUES (?,?,?)',(event_id,session,now)).rowcount
            if not inserted:return False
            old=self.db.execute('SELECT * FROM session_run WHERE id=?',(session,)).fetchone()
            started=old['burst_started'] if old and old['pending_at'] is not None and old['burst_started'] is not None else now
            pending=min(now+debounce_ms/1000,started+4)
            self.db.execute('''INSERT INTO session_run(id,epoch,bot_count,next_agent,pending_at,channel_id,tried,burst_started)
                VALUES (?,1,0,0,?,?,?,?) ON CONFLICT(id) DO UPDATE SET epoch=epoch+1,bot_count=0,
                next_agent=0,pending_at=excluded.pending_at,channel_id=excluded.channel_id,tried='[]',burst_started=excluded.burst_started''',
                (session,pending,channel,'[]',started))
        return True

    def proactive(self,session,channel,now=None):
        now=time.time() if now is None else now
        with self.db:self.db.execute('''INSERT INTO session_run(id,epoch,bot_count,next_agent,pending_at,channel_id,tried,burst_started)
            VALUES (?,1,0,0,?,?,?,?) ON CONFLICT(id) DO UPDATE SET epoch=epoch+1,bot_count=0,
            next_agent=0,pending_at=excluded.pending_at,channel_id=excluded.channel_id,tried='[]',burst_started=excluded.burst_started''',
            (session,now,channel,'[]',now))

    def state(self,session):
        row=self.db.execute('SELECT * FROM session_run WHERE id=?',(session,)).fetchone()
        return dict(row) if row else None

    def due(self,session,now=None):
        row=self.state(session['id']);now=time.time() if now is None else now
        return bool(row and row['pending_at'] is not None and row['pending_at']<=now and row['bot_count']<session.get('max_bot_messages_without_human',6))

    def remaining(self,session):
        row=self.state(session['id'])
        return max(0,session.get('max_bot_messages_without_human',6)-(row['bot_count'] if row else 0))

    def sent(self,session,agent_index,count,epoch,now=None):
        now=time.time() if now is None else now
        with self.db:
            row=self.state(session['id'])
            if not row or row['epoch']!=epoch:return False
            total=row['bot_count']+count
            self.db.execute('UPDATE session_run SET bot_count=?,next_agent=?,pending_at=?,tried=?,burst_started=NULL WHERE id=?',
                (total,agent_index,None,'[]',session['id']))
        return True

    def silent(self,session,agent_index,epoch,now=None):
        row=self.state(session['id'])
        if not row or row['epoch']!=epoch:return
        self.stop(session['id'])

    def stop(self,session):
        with self.db:self.db.execute('UPDATE session_run SET pending_at=NULL WHERE id=?',(session,))

    def resume(self,session,channel,persona):
        state=self.state(session['id'])
        if not state or not self.remaining(session) or persona!=active_persona(session):return
        with self.db:self.db.execute('UPDATE session_run SET pending_at=?,channel_id=?,next_agent=?,tried=? WHERE id=?',
            (time.time(),channel,session['persona_ids'].index(persona),'[]',session['id']))

    def remember_outgoing(self,event,session,agent):
        with self.db:self.db.execute('INSERT OR IGNORE INTO outgoing_events VALUES (?,?,?,?)',(event,session,agent,time.time()))

    def is_outgoing(self,event):
        return self.db.execute('SELECT 1 FROM outgoing_events WHERE id=?',(event,)).fetchone() is not None


class ScopedStores:
    def __init__(self,control,counter):self.control,self.counter=control,counter
    def conversation(self,session):return ConversationContext(self.control.conversation_path(session),'shared',self.counter)
    def memory(self,persona,session):return GroupMemory(self.control.memory_path(persona,session),'shared')
