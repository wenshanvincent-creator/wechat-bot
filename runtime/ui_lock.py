"""One WeChat desktop means one UI action at a time.

The old code used a non-blocking flock, so a second action (reply, bind, image
copy, Moment) failed instantly whenever the window was busy. Callers now wait
in line instead, bounded by an explicit timeout.
"""
import fcntl
import os
import time


class Busy(RuntimeError):
    pass


def wait(path, timeout=25.0, poll=0.15):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + max(1.0, float(timeout))
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise Busy('微信窗口正在处理另一个操作，请稍后重试')
            time.sleep(poll)


class Locked:
    def __init__(self, root, timeout=25.0):
        self.path = root / 'runtime.lock'
        self.timeout = timeout
        self.descriptor = None

    def __enter__(self):
        self.descriptor = wait(self.path, self.timeout)
        return self

    def __exit__(self, *_):
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)
