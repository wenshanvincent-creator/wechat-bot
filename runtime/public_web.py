"""Bounded public HTTP fetch, pinning validated DNS addresses across the connection."""
import http.client
import ipaddress
import re
import socket
import ssl
from html.parser import HTMLParser
from urllib.parse import urlsplit,urljoin


class Text(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.ignore=0;self.links=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.ignore+=1
        if tag=='a':
            href=dict(attrs).get('href','')
            if href.startswith(('https://','http://')):self.links.append(href)
    def handle_endtag(self,tag):
        if tag in ('script','style') and self.ignore:self.ignore-=1
    def handle_data(self,data):
        if not self.ignore and data.strip():self.parts.append(data.strip())


def search(query, limit=4):
    """Clean web results from Bing: title, link and a short summary."""
    import html as html_module
    from urllib.parse import quote
    data = fetch('https://www.bing.com/search?q=' + quote(query), raw=True)
    page = data.get('html') or ''
    items = []
    for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S):
        heading = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not heading:
            continue
        title = html_module.unescape(re.sub(r'<[^>]+>', '', heading.group(2))).strip()
        link = html_module.unescape(heading.group(1))
        snippet = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        summary = html_module.unescape(re.sub(r'<[^>]+>', ' ', snippet.group(1))) if snippet else ''
        summary = re.sub(r'\s+', ' ', summary).strip()
        if title and link:
            items.append({'title': title, 'link': link, 'summary': summary[:200]})
        if len(items) >= limit:
            break
    return items


def fetch(url,limit=256*1024,*,image=False,raw=False):
    for _ in range(5):
        p=urlsplit(url)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:raise ValueError('仅允许无内嵌凭据的公开 HTTP(S) 网页')
        port=p.port or (443 if p.scheme=='https' else 80)
        if port not in (80,443):raise ValueError('网页只允许 80/443 端口')
        addresses={r[4][0] for r in socket.getaddrinfo(p.hostname,port,type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):raise ValueError('不允许访问内网、回环或元数据地址')
        connection=http.client.HTTPConnection(p.hostname,port,timeout=20)
        sock=socket.create_connection((sorted(addresses)[0],port),timeout=20)
        connection.sock=ssl.create_default_context().wrap_socket(sock,server_hostname=p.hostname) if p.scheme=='https' else sock
        try:
            connection.request('GET',(p.path or '/')+('?' +p.query if p.query else ''),headers={'Host':p.hostname,'User-Agent':'WhaleBot/2.0 (+public-page-reader)','Accept':'text/html,text/plain'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):url=urljoin(url,response.getheader('Location',''));continue
            if response.status!=200:raise ValueError('网页返回 HTTP '+str(response.status))
            media=response.getheader('Content-Type','')
            if image:
                if not any(t in media for t in ('image/','application/octet-stream')):raise ValueError('媒体地址未返回图片')
                data=response.read(limit+1)
                if len(data)>limit:raise ValueError('图片超过大小限制')
                return data
            if not any(t in media for t in ('text/','application/json')):raise ValueError('网页工具仅接收文本')
            data=response.read(limit+1);encoding=re.search(r'charset=([\w-]+)',media)
            text=data[:limit].decode(encoding[1] if encoding else 'utf-8',errors='replace')
            if raw:return {'url':url,'html':text,'truncated':len(data)>limit}
            parser=Text();parser.feed(text)
            return {'url':url,'text':'\n'.join(parser.parts)[:16000],'links':list(dict.fromkeys(parser.links))[:30],'truncated':len(data)>limit}
        finally:connection.close()
    raise ValueError('网页重定向过多')
