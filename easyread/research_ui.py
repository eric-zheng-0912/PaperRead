"""Scoped research UI adapters; task ownership/commit barriers stay in tasks.py."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from types import SimpleNamespace

from . import planning_store
from .research import tasks
from .store import dir_lock
from .i18n import tr

PATH_KEY = 'research-ui:path:v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _sources(lib):
    # Read only. These fields also detect user changes while an analysis runs.
    from .background import snapshot
    return _digest([(ws.id, snapshot(ws))
                    for ws in sorted(lib.all(), key=lambda ws: ws.id)])


def latest(root):
    return planning_store.cache_get(root, PATH_KEY)[0] or {'revision': '', 'results': [], 'relations': []}


def relations(steps):
    """Only actual referenced_works evidence; order/AI reasons are not citations."""
    def ident(value):
        return str(value or '').rstrip('/').rsplit('/', 1)[-1].upper()
    nodes = {ident(s['paper'].get('openalex_id')): s['paper_key'] for s in steps
             if s['paper'].get('openalex_id')}
    edges = set()
    for step in steps:
        for ref in step['paper'].get('referenced_works') or []:
            target = nodes.get(ident(ref))
            if target and target != step['paper_key']:
                edges.add((step['paper_key'], target))
    return [{'source': a, 'target': b, 'evidence': 'referenced_works'} for a, b in sorted(edges)]


def monitor(lib):
    return {'scope': tasks.scope_id(lib.root),
            'tasks': tasks.list_tasks(scope=tasks.scope_id(lib.root), storage=tasks.storage_for(lib.root))}


def get(app, endpoint, query):
    lib = app.lib  # Capture one library, never follow a switch mid-request.
    scope = tasks.scope_id(lib.root)
    if query.get('scope') and query['scope'] != scope:
        raise ValueError('Library changed; reload the research workspace')
    if endpoint == 'ui/monitor':
        return monitor(lib)
    if endpoint == 'ui/path':
        return {'scope': scope, **latest(lib.root)}
    if endpoint.startswith('ui/peek/'):
        pid = endpoint[len('ui/peek/'):]
        ws = lib.ws(pid)
        if not ws:
            raise ValueError('Paper no longer exists')
        paper, reader = ws.load('paper') or {}, ws.load('reader') or {}
        from .research_v2 import _key
        meta = paper.get('meta') or {}
        summary = planning_store.summary(lib.root, _key({**meta, 'title': meta.get('title_en') or meta.get('title_zh')}))
        notes = sorted((n for n in reader.get('notes', {}).values() if not n.get('deleted')),
                       key=lambda n: str(n.get('updated') or n.get('created') or ''), reverse=True)
        valid = {str(b.get('id')) for b in paper.get('blocks', []) if isinstance(b, dict)}
        records = [{**n, 'anchor': str(n.get('anchor')) if str(n.get('anchor')) in valid else ''} for n in notes]
        return {'scope': scope, 'id': pid, 'meta': meta, 'summary': (summary or {}).get('summary_markdown', ''), 'item': ws.load('item') or {},
                'records': [n for n in records if n.get('kind') != 'question'][:5],
                'questions': [n for n in records if n.get('kind') == 'question'][:5]}
    return None


def post(app, endpoint, body):
    lib = app.lib
    root, scope = lib.root, tasks.scope_id(lib.root)
    if body.get('scope') != scope:
        raise ValueError('Library changed; reload the research workspace')
    if endpoint == '/ui/cancel':
        task = tasks.cancel(str(body.get('id') or ''), scope=scope, storage=tasks.storage_for(root))
        if not task:
            raise ValueError('Task no longer exists in this library')
        return {'scope': scope, 'task': task}
    if endpoint == '/ui/path/reorder':
        with dir_lock(root, '.research-ui-path.lock'):
            path = latest(root)
            keys = body.get('keys')
            expected = [s['paper_key'] for s in path['results']]
            if body.get('revision') != path['revision']:
                raise ValueError('Reading path changed; reload before saving')
            if not isinstance(keys, list) or len(keys) != len(expected) or set(keys) != set(expected):
                raise ValueError('Invalid reading order')
            by_key = {s['paper_key']: s for s in path['results']}
            path.update(results=[by_key[k] for k in keys], revision=uuid.uuid4().hex)
            planning_store.cache_put(root, PATH_KEY, path)
        return {'scope': scope, **path}
    if endpoint == '/ui/path/start':
        from . import research_v2
        captured = SimpleNamespace(lib=lib)
        request = copy.deepcopy(body.get('request') or {})
        baseline, old_revision = _sources(lib), latest(root)['revision']

        def run(cancel, report):
            report('planning', 0, 1, tr('正在規劃閱讀路徑；可繼續其他操作'))
            result = research_v2.post(captured, '/reading-path', request, _cancel=cancel)
            steps = result.get('results') or []
            if not steps:
                raise ValueError('No source-backed reading path returned')
            for step in steps:
                step['paper_key'] = research_v2._key(step['paper'])
            result.update(revision=uuid.uuid4().hex, relations=relations(steps), topic=request.get('topic', ''), goal=request.get('goal', ''))

            def save():
                # Order: task barrier -> path lock -> source locks; no inverse acquisition.
                from contextlib import ExitStack
                with dir_lock(root, '.research-ui-path.lock'), ExitStack() as stack:
                    for ws in sorted(lib.all(), key=lambda ws: ws.id):
                        stack.enter_context(dir_lock(ws.root))
                    if _sources(lib) != baseline or latest(root)['revision'] != old_revision:
                        raise ValueError('Source or reading order changed; result not saved')
                    planning_store.cache_put(root, PATH_KEY, result)
                return result
            return tasks.commit(cancel, save)

        return {'scope': scope, 'task': tasks.submit('reading-path', run, retry_failed=True,
                                                   scope=scope, storage=tasks.storage_for(root))}
    raise ValueError('Unknown research UI operation')
