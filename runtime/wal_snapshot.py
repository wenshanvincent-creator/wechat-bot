"""Read authenticated SQLCipher base/WAL snapshots without touching the originals.

WAL checksums follow SQLite's file format specification. A snapshot is per
database, not a transaction spanning different WeChat database files.
"""
import hashlib
import hmac
import os
import sqlite3
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SIZE = 4096
RESERVED = 80
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_WAL_BYTES = 64 * 1024 * 1024


class SnapshotChanged(RuntimeError):
    pass


class PageCodec:
    def __init__(self, key, salt):
        self.key = key
        self.salt = salt
        self.auth_key = hashlib.pbkdf2_hmac(
            'sha512', key, bytes(value ^ 0x3a for value in salt), 2, dklen=32)
        self.number_order = None

    def decode(self, page, number):
        if len(page) != PAGE_SIZE:
            raise ValueError('Incomplete encrypted page')
        offset = 16 if number == 1 else 0
        orders = (self.number_order,) if self.number_order else ('<I', '>I')
        for order in orders:
            expected = hmac.new(self.auth_key, page[offset:-64] + struct.pack(order, number), 'sha512').digest()
            if hmac.compare_digest(expected, page[-64:]):
                self.number_order = order
                break
        else:
            raise ValueError('Encrypted page authentication failed')
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(page[-RESERVED:-64])).decryptor()
        decoded = decryptor.update(page[offset:-RESERVED]) + decryptor.finalize()
        return (b'SQLite format 3\0' if number == 1 else b'') + decoded + bytes(RESERVED)


def checksum(data, state=(0, 0), order='<'):
    if len(data) % 8:
        raise ValueError('Invalid WAL checksum input')
    first, second = state
    for left, right in struct.iter_unpack(order + 'II', data):
        first = (first + left + second) & 0xffffffff
        second = (second + right + first) & 0xffffffff
    return first, second


def committed_frames(wal, page_size=PAGE_SIZE):
    """Return page offsets through the last valid commit and coverage metadata.

    SQLite can retain stale frames after a WAL reset. An invalid tail never
    supplies pages; an invalid header is an error, rather than an empty WAL.
    """
    if not wal:
        return {}, {'valid_frames': 0, 'committed_frames': 0, 'db_pages': None, 'tail': 'absent'}
    if len(wal) < 32:
        raise ValueError('Incomplete WAL header')
    magic, version, size, sequence, salt1, salt2, check1, check2 = struct.unpack('>8I', wal[:32])
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or size != page_size:
        raise ValueError('Unsupported WAL format')
    order = '<' if magic == 0x377f0682 else '>'
    state = checksum(wal[:24], order=order)
    if state != (check1, check2):
        raise ValueError('WAL header checksum failed')
    frames = []
    commit_end = 0
    db_pages = None
    tail = 'complete'
    offset = 32
    while offset + 24 + size <= len(wal):
        number, count, frame_salt1, frame_salt2, check1, check2 = struct.unpack('>6I', wal[offset:offset + 24])
        if (frame_salt1, frame_salt2) != (salt1, salt2):
            tail = 'different_generation'
            break
        if number == 0:
            tail = 'invalid_page_number'
            break
        frame_state = checksum(wal[offset:offset + 8], state, order)
        frame_state = checksum(wal[offset + 24:offset + 24 + size], frame_state, order)
        if frame_state != (check1, check2):
            tail = 'checksum_failure'
            break
        state = frame_state
        frames.append((number, offset + 24))
        if count:
            commit_end = len(frames)
            db_pages = count
        offset += 24 + size
    if tail == 'complete' and offset != len(wal):
        tail = 'partial_frame'
    if tail == 'complete' and len(frames) > commit_end:
        tail = 'uncommitted_frames'
    latest = {number: position for number, position in frames[:commit_end] if number <= db_pages}
    return latest, {'valid_frames': len(frames), 'committed_frames': commit_end,
                    'db_pages': db_pages, 'tail': tail, 'checkpoint_sequence': sequence,
                    'wal_generation': f'{salt1:08x}{salt2:08x}'}


def _identity(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _path_identity(path):
    try:
        return _identity(path.stat())
    except FileNotFoundError:
        return None


def _zero_range(stream, start, length):
    """Verify WCDB's observed physical preallocation, with bounded RAM."""
    stream.seek(start)
    zeros = bytes(1024 * 1024)
    while length:
        chunk = stream.read(min(len(zeros), length))
        if not chunk or chunk != zeros[:len(chunk)]:
            return False
        length -= len(chunk)
    return True


def capture(path, key):
    """Capture a stable base and WAL, authenticating every selected page.

    Concurrent writes/reset/replacement fail explicitly. We do not lock or
    retry writers, or silently fall back to a stale base checkpoint.
    """
    path = Path(path)
    wal_path = Path(str(path) + '-wal')
    wal_before = _path_identity(wal_path)
    with path.open('rb', buffering=0) as base:
        before = _identity(os.fstat(base.fileno()))
        first = base.read(PAGE_SIZE)
        codec = PageCodec(key, first[:16])
        header = codec.decode(first, 1)
        if struct.unpack('>H', header[16:18])[0] != PAGE_SIZE or header[20] != RESERVED:
            raise ValueError('Unsupported decoded page layout')
        count = struct.unpack('>I', header[28:32])[0]
        if not count or header[24:28] != header[92:96]:
            raise ValueError('Unverified base page count')
        if count * PAGE_SIZE > MAX_IMAGE_BYTES:
            raise ValueError('Base snapshot exceeds memory budget')
        if count * PAGE_SIZE > before[2]:
            raise ValueError('Incomplete base checkpoint')
        if wal_before is None:
            wal = b''
        else:
            if wal_before[2] > MAX_WAL_BYTES:
                raise ValueError('WAL exceeds memory budget')
            with wal_path.open('rb', buffering=0) as stream:
                if _identity(os.fstat(stream.fileno())) != wal_before:
                    raise SnapshotChanged('WAL changed before capture')
                wal = stream.read(MAX_WAL_BYTES + 1)
                if _identity(os.fstat(stream.fileno())) != wal_before:
                    raise SnapshotChanged('WAL changed during capture')
        frames, metadata = committed_frames(wal)
        final_count = metadata['db_pages'] or count
        if final_count * PAGE_SIZE > MAX_IMAGE_BYTES:
            raise ValueError('Merged snapshot exceeds memory budget')
        logical_count = count
        if 1 in frames:
            offset = frames[1]
            current_header = codec.decode(wal[offset:offset + PAGE_SIZE], 1)
            if current_header[24:28] != current_header[92:96]:
                raise ValueError('Unverified committed header page count')
            logical_count = struct.unpack('>I', current_header[28:32])[0]
            if not logical_count:
                raise ValueError('Invalid committed header page count')
        metadata['excluded_zero_preallocation_pages'] = 0
        # Observed WCDB layout: header says 400 logical pages, the file and
        # WAL commit say 7680 physical pages, pages 401..7680 are all zero,
        # and every committed frame targets a logical page. Do not invent
        # missing encrypted pages for that positively verified empty tail.
        if (final_count > logical_count and max(frames, default=0) <= logical_count
                and before[2] >= final_count * PAGE_SIZE
                and _zero_range(base, logical_count * PAGE_SIZE, (final_count - logical_count) * PAGE_SIZE)):
            metadata['excluded_zero_preallocation_pages'] = final_count - logical_count
            final_count = logical_count
        available = min(final_count, before[2] // PAGE_SIZE)
        base.seek(0)
        encrypted = base.read(available * PAGE_SIZE)
        if len(encrypted) != available * PAGE_SIZE:
            raise SnapshotChanged('Base shortened during capture')
        if _identity(os.fstat(base.fileno())) != before or _path_identity(path) != before:
            raise SnapshotChanged('Base changed during capture')
        if _path_identity(wal_path) != wal_before:
            raise SnapshotChanged('WAL changed during capture')
    image = bytearray(final_count * PAGE_SIZE)
    for number in range(1, final_count + 1):
        if number in frames:
            offset = frames[number]
            page = wal[offset:offset + PAGE_SIZE]
        elif number <= available:
            page = encrypted[(number - 1) * PAGE_SIZE:number * PAGE_SIZE]
        else:
            raise ValueError('Committed WAL leaves a page gap')
        image[(number - 1) * PAGE_SIZE:number * PAGE_SIZE] = codec.decode(page, number)
    # An isolated memory image has no external WAL sidecar. This header change
    # applies only to the decoded RAM copy, never to WeChat's database files.
    image[18:20] = b'\1\1'
    metadata.update({'base_pages': count, 'physical_base_pages': before[2] // PAGE_SIZE,
                     'snapshot_pages': final_count,
                     'base_sha256': hashlib.sha256(encrypted).hexdigest(),
                     'wal_sha256': hashlib.sha256(wal).hexdigest(),
                     'coverage': 'stable capture; last checksum-valid WAL commit; authenticated selected pages'})
    return image, metadata


def connect(path, key):
    image, metadata = capture(path, key)
    connection = sqlite3.connect(':memory:')
    try:
        connection.deserialize(bytes(image))
        connection.execute('PRAGMA query_only=ON')
        connection.row_factory = sqlite3.Row
    except Exception:
        connection.close()
        raise
    return connection, metadata
