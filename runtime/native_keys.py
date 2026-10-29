"""Ephemeral key discovery for the single WeChat main process in our container.

Derived from the verified P0 probe; no keys or memory buffers are persisted.
"""
import hashlib
import hmac
import re
import resource
import struct
import time
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
PROFILE=Path('/opt/wechat-server/profile')
PAGE_SIZE=4096
MAX_BYTES=256*1024*1024
MAX_SECONDS=20
RAW_KEY=re.compile(rb"x'([0-9a-fA-F]{64,192})'")


def process_identity():
    # Compose joins the desktop service's PID namespace, never the host namespace.
    # Check the sole main process and its UID/executable before reading any memory.
    matches = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            if (proc / 'comm').read_text(encoding='utf-8').strip() != 'wechat':
                continue
            status = (proc / 'status').read_text(encoding='utf-8').splitlines()
            uid = next(line.split()[1:] for line in status if line.startswith('Uid:'))
            if uid != ['1000'] * 4 or (proc / 'exe').readlink().name != 'wechat':
                raise RuntimeError('Unexpected WeChat process owner or executable')
            start = (proc / 'stat').read_text(encoding='utf-8').rsplit(')', 1)[1].split()[19]
            namespace = next(line.split(':', 1)[1].split() for line in status if line.startswith('NSpid:'))
            matches.append((int(proc.name), int(namespace[-1]), start))
        except (FileNotFoundError,ProcessLookupError):
            continue  # A process exited while the namespace was being enumerated.
    if len(matches) != 1:
        raise RuntimeError('Expected one WeChat main process in desktop PID namespace')
    return matches[0]


def encoded_candidates(blob,salts):
    """Hypothesis: a 99-byte WCDB literal XORed with a repeating 32-byte mask.

    Its final 32 hex characters are the known public DB salt. Recover a
    candidate mask from those bytes; only page HMAC can authenticate a key.
    No platform-specific XOR mask or object offset is assumed.
    """
    if len(blob)!=99: return {}
    found={}
    for salt in salts:
        known=salt.hex().encode('ascii')
        if blob[0]^blob[96]^known[30]!=ord('x') or blob[1]^blob[97]^known[31]!=ord("'") or blob[98]^blob[66]^known[0]!=ord("'"):
            continue
        mask=bytearray(32)
        for index,value in enumerate(known,66): mask[index%32]=blob[index]^value
        decoded=bytes(value^mask[index%32] for index,value in enumerate(blob))
        if RAW_KEY.fullmatch(decoded):
            found[salt]=bytes.fromhex(decoded[2:66].decode('ascii'))
    return found

def page_valid(page,key,number=1,salt=None):
    salt=page[:16] if salt is None else salt
    offset=16 if number==1 else 0
    auth_key=hashlib.pbkdf2_hmac('sha512',key,bytes(x^0x3a for x in salt),2,dklen=32)
    data=page[offset:PAGE_SIZE-64]
    return any(hmac.compare_digest(hmac.new(auth_key,data+struct.pack(order,number),'sha512').digest(),page[-64:])
               for order in ('<I','>I'))

def active_account_roots(identity):
    # An inactive account directory is not an authorized live data source.
    proc=Path('/proc')/str(identity[0])
    open_paths=set()
    for descriptor in (proc/'fd').iterdir():
        try:
            target=descriptor.readlink()
        except (FileNotFoundError,ProcessLookupError):
            continue
        if str(target).startswith('/config/'):
            open_paths.add(PROFILE/target.relative_to('/config'))
    roots=[root for root in PROFILE.rglob('db_storage') if any(path.is_relative_to(root) for path in open_paths)]
    return roots


class NativeAccountUnavailable(RuntimeError):
    pass


def current_account_root(identity):
    # Qt can close/reopen descriptors while a snapshot is being enumerated.
    # Retry only an empty result; ambiguity and process changes fail closed.
    for delay in (0, .15, .35):
        if delay:time.sleep(delay)
        if process_identity()!=identity:
            raise RuntimeError('WeChat process changed; create a new reader')
        roots=active_account_roots(identity)
        if roots:break
    if not roots:
        raise NativeAccountUnavailable('WeChat login required: no active account database. Check wechat_status.nativeAccount; do not retry each group or contact.')
    if len(roots)!=1:
        raise RuntimeError('Multiple active account database roots; do not choose an account by guesswork')
    return roots[0]


def account_reference(root):
    return hashlib.sha256(str(root.relative_to(PROFILE)).encode()).hexdigest()[:24]


def discover(identity):
    roots=[current_account_root(identity)]
    databases=[]
    for root in roots:
        for path in sorted(root.rglob('*.db')):
            # The reader uses only the directory, sessions and ordinary message
            # shards. Sending an image activates resource/FTS/chatbot databases,
            # whose unrelated keys need not be resident and must not gate reads.
            category=('contact' if path.name=='contact.db' else 'session' if path.name=='session.db'
                      else 'message' if re.fullmatch(r'message_\d+\.db',path.name) else None)
            if category is None: continue
            with path.open('rb') as stream: page=stream.read(PAGE_SIZE)
            if len(page)!=PAGE_SIZE or page.startswith(b'SQLite format 3\0'): continue
            databases.append({'path':path,'page':page,'category':category,
                              'file_ref':hashlib.sha256(str(path.relative_to(PROFILE)).encode()).hexdigest()[:16]})
    return databases

def scan(databases, identity):
    PID,NAMESPACE_PID,START_TICKS=identity
    proc=Path('/proc')/str(PID)
    if (proc/'comm').read_text().strip()!='wechat': raise RuntimeError('Unexpected process')
    if (proc/'stat').read_text().rsplit(')',1)[1].split()[19]!=START_TICKS: raise RuntimeError('Process identity changed')
    namespace=next(line.split(':',1)[1].split() for line in (proc/'status').read_text().splitlines() if line.startswith('NSpid:'))
    if int(namespace[-1])!=NAMESPACE_PID: raise RuntimeError('Process namespace changed')
    regions=[]; region=None
    for line in (proc/'smaps').read_text().splitlines():
        if re.match(r'^[0-9a-f]+-[0-9a-f]+ ',line):
            parts=line.split(maxsplit=5); label=parts[5] if len(parts)>5 else ''
            start,end=(int(x,16) for x in parts[0].split('-'))
            eligible='r' in parts[1] and 'w' in parts[1] and (not label or label=='[heap]' or label.startswith('[anon'))
            region={'start':start,'end':end,'eligible':eligible,'rss':0}
            regions.append(region)
        elif region is not None and line.startswith('Rss:'):
            region['rss']=int(line.split()[1])
    regions=sorted((r for r in regions if r['eligible'] and r['rss'] and r['end']-r['start']<=MAX_BYTES),
                   key=lambda r:r['rss'],reverse=True)
    salts={item['page'][:16] for item in databases}
    readable=[]
    for line in (proc/'maps').read_text().splitlines():
        parts=line.split()
        if 'r' in parts[1]: readable.append(tuple(int(x,16) for x in parts[0].split('-')))
    def read_pointer(memory,address,length):
        if not any(start<=address and address+length<=end for start,end in readable): return b''
        try: return __import__('os').pread(memory.fileno(),length,address)
        except OSError: return b''
    found={}; seen=set(); seen_pointers=set(); bytes_read=0; candidates=0; blob_count=0; started=time.monotonic()
    with (proc/'mem').open('rb',buffering=0) as memory:
        for region in regions:
            start,end=region['start'],region['end']
            position=start; carry=b''
            while position<end and bytes_read<MAX_BYTES and time.monotonic()-started<MAX_SECONDS:
                try:
                    memory.seek(position); chunk=memory.read(min(1024*1024,end-position,MAX_BYTES-bytes_read))
                except OSError: break
                if not chunk: break
                bytes_read+=len(chunk); position+=len(chunk)
                data=carry+chunk
                key_candidates=[]
                # WCDB UnsafeData publicly declares adjacent buffer/size
                # fields. Inspect only mapped buffers whose stored size is 99.
                marker=struct.pack('<Q',99); offset=data.find(marker)
                while offset>=0:
                    address=position-len(chunk)-len(carry)+offset
                    if address%8==0:
                        pointer_bytes=read_pointer(memory,address-8,8)
                        if len(pointer_bytes)==8:
                            pointer=struct.unpack('<Q',pointer_bytes)[0]
                            if pointer not in seen_pointers:
                                seen_pointers.add(pointer)
                                blob=read_pointer(memory,pointer,99)
                                if len(blob)==99:
                                    blob_count+=1
                                    key_candidates.extend(encoded_candidates(blob,salts).values())
                    offset=data.find(marker,offset+1)
                for key in key_candidates:
                    if key in seen: continue
                    seen.add(key); candidates+=1
                    for item in databases:
                        reference=item['file_ref']
                        if reference not in found and page_valid(item['page'],key): found[reference]=key
                carry=chunk[-256:]
                if len(found)==len(databases): break
            if len(found)==len(databases) or bytes_read>=MAX_BYTES or time.monotonic()-started>=MAX_SECONDS: break
    return found,{'bytes_read':bytes_read,'candidate_count':candidates,'seconds':round(time.monotonic()-started,3),
                  'coverage_strategy':'resident-first anonymous writable regions; zero-RSS regions skipped',
                  'candidate_mode':'WCDB buffer-size 99 and salt-derived repeating-mask hypothesis',
                  'mapped_blob99_count':blob_count,
                  'all_requested_keys_found':len(found)==len(databases)}
