"""JSON stdin/stdout bridge for the private MCP process. No raw-data logging."""
import json
import sys
from pathlib import Path

from consumer_state import ConsumerState
from native_reader import NativeReader

MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def dispatch(command, arguments):
    reader = NativeReader()
    if command == 'groups':
        return reader.list_groups()
    if command == 'messages':
        return reader.read_messages(**arguments)
    if command == 'unread':
        return reader.read_unread(**arguments)
    if command in ('consumer_start', 'consumer_poll', 'consumer_ack'):
        path = Path('/opt/wechat-server/mcp-state/structured-data/consumer-progress.sqlite')
        ledger = ConsumerState(path)
        try:
            method = {'consumer_start': ledger.start, 'consumer_poll': ledger.poll, 'consumer_ack': ledger.acknowledge}[command]
            return method(reader, **arguments)
        finally:
            ledger.close()
    raise ValueError('Unknown native data operation')


def main():
    try:
        request = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(request) > MAX_REQUEST_BYTES:
            raise ValueError('Native request exceeds size limit')
        arguments = json.loads(request)
        response = json.dumps({'ok': True, 'data': dispatch(sys.argv[1], arguments)}, ensure_ascii=False).encode()
        if len(response) > MAX_RESPONSE_BYTES:
            raise ValueError('Native response exceeds budget; reduce page size')
    except Exception as error:
        # Do not serialize codec errors (which can include private source bytes)
        # or traceback/frame locals. The error type distinguishes transient
        # snapshot change, invalid input, unsupported encoding and runtime faults.
        message = str(error) if type(error) in (ValueError, RuntimeError) else 'Native data operation failed'
        response = json.dumps({'ok': False, 'error_type': type(error).__name__, 'message': message}).encode()
    sys.stdout.buffer.write(response + b'\n')


if __name__ == '__main__':
    main()
