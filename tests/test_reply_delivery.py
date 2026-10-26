import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from policy import Ledger
from reply_delivery import deliver,transport_parts


def test_ordered_bubbles_have_unique_durable_receipts(tmp_path):
    ledger=Ledger(tmp_path/'ledger.sqlite')
    ledger.record('parent','group','checking',0,'',{'timezone':'UTC'})
    calls=[]
    def sender(group,text,op,account,deadline):
        calls.append((text,op)); return {'status':'sent'}
    receipt=deliver(ledger,'group','parent',['第一条','第二条'],'account',sender)
    assert receipt['sent_parts']==2 and receipt['status']=='sent'
    assert [c[0] for c in calls]==['第一条','第二条']
    assert calls[0][1]!=calls[1][1]
    assert ledger.db.execute('SELECT count(*) FROM reply_parts').fetchone()[0]==2
    assert ledger.can_send('group')


def test_uncertain_part_stops_the_rest_without_resend(tmp_path):
    ledger=Ledger(tmp_path/'ledger.sqlite')
    ledger.record('parent','group','checking',0,'',{'timezone':'UTC'})
    calls=[]
    def sender(group,text,*args):
        calls.append(text); return {'status':'sent' if len(calls)==1 else 'uncertain'}
    receipt=deliver(ledger,'group','parent',['one','two','three'],'account',sender)
    assert calls==['one','two'] and receipt['status']=='uncertain'
    assert not ledger.can_send('group') and ledger.seen('parent')


def test_oversize_bubble_is_split_at_actual_ui_boundary_without_losing_text():
    parts=transport_parts(['字'*4500])
    assert [len(p) for p in parts]==[2000,2000,500]
    assert ''.join(parts)=='字'*4500
