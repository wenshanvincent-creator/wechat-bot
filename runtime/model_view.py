"""Group nicknames and a minimal model-facing view; native receipts stay private."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def protobuf_fields(data):
    offset = 0
    def varint():
        nonlocal offset
        value = shift = 0
        while True:
            byte = data[offset]; offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7
    while offset < len(data):
        tag = varint(); field, wire = tag >> 3, tag & 7
        if wire == 0:
            yield field, varint()
        elif wire == 2:
            size = varint(); value = data[offset:offset+size]; offset += size
            yield field, value
        elif wire in (1,5):
            offset += 8 if wire == 1 else 4
        else:
            raise ValueError('Unsupported native group-directory wire type')


def group_nicknames(buffer):
    # Verified against the current Linux chat_room.ext_buffer and visible group
    # names: repeated field 1 is a member, member fields 1/2 are username/nickname.
    result = {}
    for field, member in protobuf_fields(buffer):
        if field == 1 and isinstance(member, bytes):
            parts = dict(protobuf_fields(member))
            result[parts[1].decode('utf-8')] = parts.get(2,b'').decode('utf-8')
    return result


class GroupView:
    def __init__(self, labels=None, self_id='', timezone='Asia/Shanghai'):
        self.labels = dict(labels or {})
        self.self_id, self.timezone = self_id, timezone
        # Kernel passes the active persona id here so its own lines can be told
        # apart from lines written by a persona that has since been switched out.
        self.agent_id = self_id
        self.unknown = {}

    @classmethod
    def from_reader(cls, reader, group, self_id, timezone='Asia/Shanghai'):
        connection,_ = reader._open(reader._database('contact.db'))
        try:
            if not group.endswith('@chatroom'):
                row=connection.execute('SELECT nick_name FROM contact WHERE username=?',(group,)).fetchone()
                return cls({group:row[0] if row else ''},self_id,timezone)
            row = connection.execute('SELECT ext_buffer FROM chat_room WHERE username=?',(group,)).fetchone()
            members = group_nicknames(row[0]) if row and row[0] else {}
            ids = list(members)
            names = {}
            if ids:
                placeholders = ','.join('?' for _ in ids)
                names = dict(connection.execute('SELECT username,nick_name FROM contact WHERE username IN ('+placeholders+')',ids))
            return cls({sid: nickname or names.get(sid) or '' for sid,nickname in members.items()},self_id,timezone)
        finally:
            connection.close()

    def name(self, sender):
        if sender == self.self_id and sender:
            return (self.labels.get(sender) or '当前人设') + '（你）'
        if self.labels.get(sender):
            # Accounts decide identity; the nickname is only how you speak to
            # them. Two accounts can share a nickname, so the second one carries
            # a marker for the model's own bookkeeping, not for the transcript.
            same = sorted(sid for sid,label in self.labels.items() if label == self.labels[sender])
            position = same.index(sender)
            return self.labels[sender] + (f'（同名{position+1}）' if len(same) > 1 and position else '')
        if sender not in self.unknown:
            self.unknown[sender] = f'群友{len(self.unknown)+1}'
        return self.unknown[sender]

    def text(self, text, sender):
        prefix = (sender or '') + ':\n'
        return text[len(prefix):] if sender and text.startswith(prefix) else text

    def messages(self, messages):
        result = []
        for m in messages:
            if not m.get('text'):
                continue
            sender = m.get('sender_id',m.get('sender',''))
            speaker = m.get('display_name') or self.name(sender)
            agent = m.get('agent_id')
            if agent:
                # Without this the model reads the previous persona's lines as
                # its own voice and keeps imitating them after a switch.
                if agent != self.agent_id:
                    speaker += '（另一个人设，不是你）'
                elif not speaker.endswith('（你）'):
                    speaker += '（你）'
            item = {'speaker': speaker, 'text': self.text(m['text'],sender), 'sender_id': sender}
            if agent:item['agent_id']=agent
            if m.get('channel_name'):item['chat']=m['channel_name']
            if m.get('sent_at'):
                item['time'] = datetime.fromisoformat(m['sent_at']).astimezone(ZoneInfo(self.timezone)).strftime('%m-%d %H:%M')
            result.append(item)
        return result

    def digest(self, text):
        return re.sub(r'wxid_[A-Za-z0-9_]+',lambda match:self.name(match[0]),text)

    def memories(self, memories):
        return [self.name(m['subject_id'])+'：'+self.digest(m['fact']) for m in memories]


def transcript(messages):
    """One self-describing line per message: chat, account, nickname, text.

    The model then knows where and from whom a line came without any extra
    reminder in the prompt, which is what keeps several chats in one session
    from blurring together.
    """
    view = GroupView()
    lines = []
    for m in messages:
        if not m.get('text'):
            continue
        sender = m.get('sender_id') or m.get('sender') or ''
        speaker = m.get('speaker') or view.name(sender)
        account = '你' if m.get('agent_id') else (sender or '未知')
        if m.get('agent_id') and speaker.endswith('（你）'):speaker=speaker[:-3]
        head = ''
        if m.get('chat'):
            head += '聊天:' + m['chat'] + '｜'
        head += '微信号:' + account + '｜昵称:' + speaker
        when = (m.get('time') + ' ') if m.get('time') else ''
        lines.append((when + '[' + head + '] ' + m['text']).strip())
    return '\n'.join(lines)
