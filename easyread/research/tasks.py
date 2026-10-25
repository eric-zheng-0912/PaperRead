"""Small in-process task registry for potentially slow local indexing work."""
from __future__ import annotations

import threading
import time
import uuid
import copy
import hashlib
import os
from contextvars import ContextVar
from contextlib import nullcontext
from pathlib import Path
from ..background import CancelEvent
from ..engines import Cancelled
from ..store import dir_lock, read_json, write_json_atomic

from ..i18n import tr


_LOCK = threading.RLock()
_SESSION = uuid.uuid4().hex
_RUNNING = ContextVar('research_task', default=None)
_TASKS: dict[str, dict] = {}
_CANCEL: dict[str, threading.Event] = {}


def _public(task: dict) -> dict:
    return copy.deepcopy({key: value for key, value in task.items() if not key.startswith('_')})


def scope_id(root) -> str:
    return hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()[:20]


def storage_for(root):
    return Path(root) / '.background-research-jobs.json'


def _persist(task):
    path = task.get('_storage')
    if not path:
        return
    path = Path(path)
    with dir_lock(path.parent, '.background-research.lock'):
        data = read_json(path, {'version':1,'tasks':{}})
        old = data['tasks'].get(task['id'])
        if old and old.get('_session') != task.get('_session'):
            return  # Another process/restart revoked this worker.
        data['tasks'][task['id']] = {k:v for k,v in task.items() if k != '_storage'}
        write_json_atomic(path, data)


def _load(storage):
    if storage is None:
        return
    path = Path(storage)
    with dir_lock(path.parent, '.background-research.lock'):
        data = read_json(path, {'version':1,'tasks':{}})
        if not isinstance(data, dict) or not isinstance(data.get('tasks'), dict):
            raise ValueError('Invalid research status; original file preserved')
        changed = False
        for identity, saved in data['tasks'].items():
            if identity in _TASKS and saved.get('_session') == _SESSION:
                continue
            saved = dict(saved)
            if saved['state'] in ('queued','running','cancelling'):
                saved.update(state='cancelled', phase='interrupted', message='Interrupted; retry explicitly',
                             updated=time.time(), _session=_SESSION)
                data['tasks'][identity] = saved
                changed = True
            _TASKS[identity] = {**saved, '_storage':str(path)}
        if changed:
            write_json_atomic(path, data)


def _expire(scope=None):
    for task in _TASKS.values():
        if ((scope is None or task.get("scope", "") == scope)
                and task["state"] in ("queued","running","cancelling")
                and task.get("expires", float("inf")) < time.time()):
            event = _CANCEL.get(task["id"])
            if event:
                event.set()
            task.update(state="cancelled", phase="expired", message="Expired; result not saved", updated=time.time())
            _persist(task)


def commit(cancel_event, callback, validate=None):
    """Local commit barrier, not a claim that a remote request was aborted."""
    task = _RUNNING.get()
    gate = getattr(cancel_event, 'gate', _LOCK)
    path = Path(task['_storage']) if task and task.get('_storage') else None
    with gate, (dir_lock(path.parent, '.background-research.lock') if path else nullcontext()):
        if cancel_event.is_set():
            raise Cancelled()
        if task:
            if task.get('expires', float('inf')) < time.time():
                raise Cancelled()
            if path:
                current = read_json(path)['tasks'].get(task['id'], {})
                if current.get('_session') != _SESSION or current.get('state') not in ('queued','running'):
                    raise Cancelled()
        if validate:
            validate()
        return callback()


def submit(kind: str, runner, retry_failed: bool = False, *, scope='', storage=None, ttl=21600) -> dict:
    with _LOCK:
        _load(storage)
        _expire(scope)
        active = next((task for task in _TASKS.values()
                       if task.get('scope','') == scope and task["kind"] == kind and task["state"] in ("queued", "running", "cancelling")), None)
        if active:
            return _public(active)
        recent_failure = next((task for task in _TASKS.values()
                               if task.get('scope','') == scope and task["kind"] == kind and task["state"] in ("failed", "cancelled")
                               and time.time() - task["updated"] < 60), None)
        if recent_failure and not retry_failed:
            return _public(recent_failure)
        expired = sorted((task for task in _TASKS.values() if task["state"] not in ("queued", "running", "cancelling")),
                         key=lambda item: item["updated"], reverse=True)[100:]
        for old in expired:
            _TASKS.pop(old["id"], None)
        identity = str(uuid.uuid4())
        task = {"id": identity, "kind": kind, "state": "queued", "phase": "queued", "done": 0,
                "total": 0, "message": tr("排入背景工作…"), "created": time.time(), "updated": time.time()}
        task.update(scope=scope, expires=time.time()+ttl, _session=_SESSION,
                    _storage=str(storage) if storage else None)
        cancel = CancelEvent()
        _TASKS[identity] = task
        _CANCEL[identity] = cancel
        _persist(task)

    def report(phase: str, done: int, total: int, message: str):
        with _LOCK:
            if task["state"] in ("cancelling", "cancelled"):
                return
            task.update(phase=phase, done=max(0, int(done or 0)), total=max(0, int(total or 0)),
                        message=str(message or "")[:240], updated=time.time())
            _persist(task)

    def work():
        with _LOCK:
            if cancel.is_set():
                task.update(state='cancelled', phase='cancelled')
                _persist(task)
                _CANCEL.pop(identity, None)
                return
            task.update(state="running", phase="starting", message=tr("背景工作開始"), updated=time.time())
            _persist(task)
        token = _RUNNING.set(task)
        try:
            result = runner(cancel, report)
            with _LOCK:
                if cancel.is_set() or task["expires"] < time.time():
                    task.update(state="cancelled", phase="cancelled", message=tr("工作已取消"))
                else:
                    task.update(state="completed", phase="completed", message=tr("工作完成"), result=result)
                task["updated"] = time.time()
                _persist(task)
        except Exception as error:  # noqa: BLE001
            with _LOCK:
                task.update(state="cancelled" if cancel.is_set() else "failed",
                            phase="cancelled" if cancel.is_set() else "failed",
                            message=tr("工作已取消") if cancel.is_set() else str(error)[:500], updated=time.time())
                _persist(task)
        finally:
            _RUNNING.reset(token)
            with _LOCK:
                _CANCEL.pop(identity, None)

    threading.Thread(target=work, name=f"research-{kind}-{identity[:8]}", daemon=True).start()
    return _public(task)


def get(identity: str, *, scope=None, storage=None) -> dict | None:
    with _LOCK:
        _load(storage)
        _expire(scope)
        task = _TASKS.get(identity)
        return _public(task) if task and (scope is None or task.get('scope','') == scope) else None


def list_tasks(*, scope=None, storage=None) -> list[dict]:
    with _LOCK:
        _load(storage)
        _expire(scope)
        return [_public(task) for task in sorted(_TASKS.values(), key=lambda item: item["created"], reverse=True)
                if scope is None or task.get('scope','') == scope][:20]


def cancel(identity: str, *, scope=None, storage=None) -> dict | None:
    with _LOCK:
        _load(storage)
        _expire(scope)
        task = _TASKS.get(identity)
        event = _CANCEL.get(identity)
        if not task or (scope is not None and task.get('scope','') != scope):
            return None
        if event and task["state"] in ("queued", "running"):
            event.set()
            task.update(state="cancelling", phase="cancelling", message=tr("正在取消工作…"), updated=time.time())
            _persist(task)
        return _public(task)
