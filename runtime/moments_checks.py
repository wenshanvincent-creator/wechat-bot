"""Background-independent control checks and strict native post receipts."""
import math
import xml.etree.ElementTree as ET
from PIL import ImageChops,ImageStat


def icon_matches(actual,expected):
    if actual.size!=expected.size:return False
    a=list(actual.convert('L').tobytes());b=list(expected.convert('L').tobytes())
    ma=sum(a)/len(a);mb=sum(b)/len(b)
    va=sum((v-ma)**2 for v in a);vb=sum((v-mb)**2 for v in b)
    if min(va,vb)<len(a)*64:return False
    return sum((x-ma)*(y-mb) for x,y in zip(a,b))/math.sqrt(va*vb)>=.94


def vertical_matches(pane,expected):
    if pane.width!=expected.width or pane.height<expected.height:return []
    return [y for y in range(pane.height-expected.height+1) if sum(ImageStat.Stat(ImageChops.difference(pane.crop((0,y,pane.width,y+expected.height)).convert('RGB'),expected.convert('RGB'))).mean)/3<=1.5]


def native_post(tid,content,self_id):
    try:
        root=ET.fromstring(content);obj=root if root.tag=='TimelineObject' else root.find('TimelineObject')
        if obj is None or obj.findtext('username')!=self_id:return None
        identifier=obj.findtext('id','');private=obj.findtext('private')
        if not identifier.isdigit() or int(identifier)<=0 or private not in ('0','1'):return None
        return {'id':identifier,'local_tid':str(tid),'text':obj.findtext('contentDesc') or '',
                'created':int(obj.findtext('createTime') or '0'),'visibility':'private' if private=='1' else 'friends'}
    except (ET.ParseError,ValueError,TypeError):return None


def matching_post(rows,before,text,visibility,started):
    found=[r for r in rows if r['id'] not in before and r['text']==text and r['visibility']==visibility and r['created']>=started-2]
    return found[0] if len(found)==1 else None
