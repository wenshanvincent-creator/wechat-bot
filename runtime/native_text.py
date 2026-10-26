"""WCDB text decoding using the host's existing libzstd, without installation.

Flag interpretation: pinned Tencent WCDB CompressionConst.hpp. Dictionary
compressed fields are explicit unsupported values, not silently empty text.
"""
import ctypes
import ctypes.util

MAX_TEXT_BYTES = 4 * 1024 * 1024
_library = None


def _zstd():
    global _library
    if _library is None:
        _library = ctypes.CDLL(ctypes.util.find_library('zstd'))
        _library.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        _library.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        _library.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        _library.ZSTD_decompress.restype = ctypes.c_size_t
        _library.ZSTD_isError.argtypes = [ctypes.c_size_t]
        _library.ZSTD_isError.restype = ctypes.c_uint
    return _library


def decode_text(body, flag):
    if isinstance(body, str):
        return body, 'decoded_native_text'
    if body is None:
        return None, 'native_null_content'
    if flag != 4 or not isinstance(body, bytes):
        return None, 'unsupported_native_encoding'
    library = _zstd()
    source = ctypes.create_string_buffer(body)
    size = library.ZSTD_getFrameContentSize(source, len(body))
    if size > MAX_TEXT_BYTES:
        raise ValueError('Zstd content size is unknown, invalid, or exceeds text budget')
    destination = ctypes.create_string_buffer(size)
    count = library.ZSTD_decompress(destination, size, source, len(body))
    if library.ZSTD_isError(count) or count != size:
        raise ValueError('Native Zstd text could not be decoded')
    return destination.raw[:count].decode('utf-8'), 'decoded_native_zstd_text'
