"""One-time copy of the selected legacy group into the initial v2 scope."""
import json
import sqlite3
import time
from agent_state import ScopedStores


def migrate(control,counter,legacy,view):
    if control.meta('v1_migrated'):return
    backup=control.root/'backups'/('v1-'+str(int(time.time())));backup.mkdir(parents=True)
    for name in ('context.sqlite','group-memory.sqlite','consumer.sqlite','decisions.sqlite'):
        path=control.root/name
        if not path.exists():continue
        source=sqlite3.connect(path);dest=sqlite3.connect(backup/name);source.backup(dest);dest.close();source.close()
    scopes=ScopedStores(control,counter);conversation=scopes.conversation('main');memory=scopes.memory('whale','main')
    account=legacy['account_ref'];groups=legacy['groups'];count=0
    if (backup/'context.sqlite').exists():
        source=sqlite3.connect(backup/'context.sqlite')
        for group in groups:
            summary=source.execute('SELECT summary FROM summaries WHERE account=? AND group_id=?',(account,group)).fetchone()
            rows=source.execute('SELECT payload FROM turns WHERE account=? AND group_id=? AND payload IS NOT NULL ORDER BY rowid',(account,group))
            messages=[]
            for row in rows:
                m=json.loads(row[0]);m['sender_id']=m.pop('sender')
                if m['sender_id']==legacy['self_sender_id']:
                    m.update(sender_id='agent:whale',agent_id='whale',display_name=control.get('personas','whale')['name'])
                else:m['display_name']=view.name(m['sender_id'])
                messages.append(m)
            conversation.append('main',messages);count+=len(messages)
            if summary and summary[0]:
                with conversation.db:conversation.db.execute('UPDATE summaries SET summary=? WHERE account=? AND group_id=?',(view.digest(summary[0]),'shared','main'))
        source.close()
    if (backup/'group-memory.sqlite').exists():
        source=sqlite3.connect(backup/'group-memory.sqlite')
        for group in groups:
            rows=source.execute('SELECT id,content,updated FROM facts WHERE account=? AND group_id=?',(account,group)).fetchall()
            with memory.db:memory.db.executemany('INSERT OR IGNORE INTO facts VALUES (?,?,?,?,?)',[('shared','main',*row) for row in rows])
        source.close()
    conversation.db.close();memory.db.close()
    control.set_meta('v1_migrated',{'at':time.time(),'messages':count,'backup':str(backup)})


def attribute_legacy_agent(control):
    """Existing v1 imports used the physical account's 'you' label for all agents."""
    if not control.meta('v1_migrated') or control.meta('legacy_agent_attribution'):return
    path=control.conversation_path('main')
    if not path.exists():return
    source=sqlite3.connect(path);self_id=control.meta('account',{}).get('self_sender_id');name=control.get('personas','whale')['name'];old_labels=set();changed=0
    with source:
        for mid,payload in source.execute("SELECT message_id,payload FROM turns WHERE account='shared' AND group_id='main' AND payload IS NOT NULL").fetchall():
            m=json.loads(payload)
            if m.get('sender')!=self_id or m.get('agent_id'):continue
            if m.get('display_name'):old_labels.add(m['display_name'])
            m.update(sender='agent:whale',agent_id='whale',display_name=name)
            source.execute("UPDATE turns SET payload=? WHERE account='shared' AND group_id='main' AND message_id=?",(json.dumps(m,ensure_ascii=False),mid));changed+=1
        row=source.execute("SELECT summary FROM summaries WHERE account='shared' AND group_id='main'").fetchone()
        if row:
            text=row[0]
            for label in old_labels:text=text.replace(label,name)
            source.execute("UPDATE summaries SET summary=? WHERE account='shared' AND group_id='main'",(text,))
    source.close();control.set_meta('legacy_agent_attribution',{'changed':changed,'at':time.time()})
