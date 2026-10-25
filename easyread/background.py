"""Optimistic write fences for background work, using existing workspace locks."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import threading
import time
import uuid
from contextlib import nullcontext

_CURRENT = ContextVar('paperread_background_fence', default=None)


class StaleWork(ValueError):
    pass


class CancelEvent(threading.Event):
    def __init__(self):
        super().__init__()
        self.gate = threading.RLock()

    def set(self):
        with self.gate:
            super().set()


def digest(path):
    h = hashlib.sha256()
    try:
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None


def snapshot(ws):
    reader = dict(ws.load('reader') or {})
    reader.pop('progress', None)  # Reading progress is not authored content.
    reader.pop('rev', None)  # Progress-only operations also increment rev.
    return {'paper': digest(ws.paper_path),
            'reader': hashlib.sha256(json.dumps(reader, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            'discussion': digest(ws.discussion_path),
            'pdf': digest(ws.source_pdf),
            'links': digest(ws.root / 'manual_table_links.json')}


class Fence:
    def __init__(self, ws, cancel, expected=None, owner=None, expires=None, registry=None):
        self.ws, self.cancel, self.owner = ws, cancel, owner
        self.expires = expires if expires is not None else float("inf")
        self.registry, self.archived = registry, False
        self.expected = dict(expected if expected is not None else snapshot(ws))
        self.gate = getattr(cancel, 'gate', threading.RLock())

    def check(self):
        from .engines import Cancelled
        if self.cancel.is_set():
            raise Cancelled()
        if time.time() > self.expires:
            raise StaleWork("Background job expired; result not saved")
        if self.owner is not None and not self.owner():
            raise StaleWork('Background job ownership changed; result not saved')
        if snapshot(self.ws) != self.expected:
            raise StaleWork('Source or authored data changed; background result not saved')


@contextmanager
def bind(fence):
    token = _CURRENT.set(fence)
    try:
        fence.check()
        yield fence
    finally:
        _CURRENT.reset(token)


@contextmanager
def suspend():
    token = _CURRENT.set(None)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def writing(ws, name):
    """Called inside dir_lock, before mutation/journal/snapshot/atomic replace."""
    fence = _CURRENT.get()
    if fence is None:
        yield
        return
    if ws.root != fence.ws.root:
        raise StaleWork('Background write belongs to another workspace')
    from .store import dir_lock
    with fence.gate, (dir_lock(fence.registry, '.background-small.lock') if fence.registry else nullcontext()):
        fence.check()
        if name == 'paper' and not fence.archived and ws.paper_path.exists():
            # One immutable pre-job paper snapshot, including prior translations
            # and caches. Never replace or prune an existing history file.
            history = ws.root / 'history'
            history.mkdir(exist_ok=True)
            path = history / f'background-{time.time_ns()}-{uuid.uuid4().hex}-paper.json'
            with path.open('xb') as stream:
                stream.write(ws.paper_path.read_bytes())
                stream.flush()
                import os
                os.fsync(stream.fileno())
            fence.archived = True
        yield
        fence.expected = snapshot(ws)
