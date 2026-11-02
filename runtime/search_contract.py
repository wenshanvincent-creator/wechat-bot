"""Current-turn evidence for searches; promises never imply background work."""
import re

SEARCH=re.compile(r'搜|联网.{0,4}查|查(?:一下|查).{0,12}(?:新闻|天气|资料)')
DELAY=re.compile(r'(?:\d+|[一二两三四五六七八九十半]+)(?:(?:秒钟?|分钟|小时|天|周)(?:之|以)?后|点)|明天|明早|今晚|稍后|等会|过会|到时|定时')
PROMISE=re.compile(r'搜着呢|正在搜|这就搜|马上搜|去搜|搜完.{0,6}(?:告诉|发)|稍后.{0,6}(?:结果|发你)')


class SearchContract:
    def __init__(self,text):
        self.required=set();self.events=[]
        for clause in re.split(r'[，,。；;\n]|然后|以及|并且',text or ''):
            if not SEARCH.search(clause) or re.search(r'(?:不用|不要|别|停止|取消).{0,5}(?:搜|联网查)',clause):continue
            if re.search(r'搜索(?:功能|状态|工具|服务|能力|权限)',clause) and not re.search(r'帮我|请|搜一下',clause):continue
            delay=DELAY.search(clause)
            self.required.add('schedule' if delay and delay.start()<SEARCH.search(clause).start() else 'instant')

    def record(self,name,result=None,error=None):
        if name not in ('web_search','class_search_schedule'):return
        ok=error is None and bool(result and (result.get('results') if name=='web_search' else result.get('ok') and result.get('id')))
        self.events.append({'kind':'instant' if name=='web_search' else 'schedule','ok':ok,'result':result or {}})

    def successful(self,kind):return [e['result'] for e in self.events if e['kind']==kind and e['ok']]

    def issue(self,output):
        if not self.required:return ''
        text='\n'.join(item for item in output if isinstance(item,str))
        clarify=bool(re.search(r'[?？]',text) and re.search(r'哪|什么|几点|何时|城市|地区|地点|时间',text) and not PROMISE.search(text))
        problems=[]
        for kind in sorted(self.required):
            successes=self.successful(kind)
            if not successes:
                attempted=any(e['kind']==kind for e in self.events)
                if clarify:continue
                if attempted and re.search(r'失败|没搜到|没成功|未完成|无法|暂时不能|没能|未创建',text) and not PROMISE.search(text):continue
                problems.append('即时搜索尚无成功结果' if kind=='instant' else '定时搜索尚无创建成功的任务编号')
            elif kind=='instant':
                links=[r.get('link','') for event in successes for r in event.get('results',[])]
                if PROMISE.search(text) or not any(link and link in text for link in links):problems.append('用已有结果给结论并附实际来源链接，不要再说正在搜')
            elif PROMISE.search(text) or not any(str(event['run_at']) in text for event in successes if event.get('run_at')):
                problems.append('按已创建任务返回的实际日期时间确认定时搜索')
        return '；'.join(problems)

    def fallback(self):
        parts=[]
        for kind in sorted(self.required):
            successes=self.successful(kind)
            if successes and kind=='instant':
                parts.append('搜到这些：\n'+'\n'.join(str(r.get('title',''))[:160]+'：'+str(r.get('summary',''))[:180]+'\n来源：'+r['link'] for r in successes[-1]['results'][:2]))
            elif successes:
                parts.extend('定时搜索已创建：'+str(r.get('run_at',''))+'，'+str(r.get('channel','当前聊天'))+'，任务编号 '+str(r['id']) for r in successes[-1:])
            else:
                attempted=any(e['kind']==kind for e in self.events)
                parts.append(('这次联网搜索失败，暂未拿到可靠结果' if attempted else '这轮没有完成联网搜索，未拿到可核实的结果') if kind=='instant' else '定时搜索未创建成功，目前没有任务会到点执行')
        return ['\n'.join(parts)]
