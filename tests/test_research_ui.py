"""Isolated research UI persistence and stale-result checks; no model/network."""
import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from easyread import planning_store, research_ui, research_v2, store
from easyread.library import Library
from easyread.research import tasks


class ResearchUITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lib = Library(Path(self.temp.name))
        root = self.lib.root / 'test001'; root.mkdir()
        store.write_json_atomic(root/'paper.json', {'meta': {'title_en': '<img src=x onerror=alert(1)>'},
                                                  'blocks': [{'id': 'body', 'type': 'para', 'en': 'Keep', 'zh': '保留'}]})
        self.ws = store.Workspace(root)
        self.ws.source_pdf.write_bytes(b'%PDF-synthetic')
        self.ws.apply_reader_ops([{'op': 'note', 'note': {'id': 'n1', 'anchor': 'body', 'body': '<script>bad</script>'}},
                                  {'op': 'note', 'note': {'id': 'q1', 'kind': 'question', 'anchor': 'gone', 'body': 'Why?'}}])
        self.app = SimpleNamespace(lib=self.lib)
        self.scope = tasks.scope_id(self.lib.root)
        self.result = {'results': [{'paper_key': 'openalex:W1', 'paper': {'title': 'One', 'openalex_id': 'W1', 'referenced_works': ['W2']}, 'reason': 'First'},
                                   {'paper_key': 'openalex:W2', 'paper': {'title': 'Two', 'openalex_id': 'W2'}, 'reason': 'Next'}]}

    def post(self, endpoint, **body):
        return research_ui.post(self.app, endpoint, {'scope': self.scope, **body})

    def wait(self, task):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = tasks.get(task['id'], scope=self.scope, storage=tasks.storage_for(self.lib.root))
            if current['state'] not in ('queued', 'running', 'cancelling'):
                return current
            time.sleep(.01)
        self.fail('Worker did not finish')

    def start(self, effect=None):
        with patch.object(research_v2, 'post', side_effect=effect or (lambda *a, **kw: copy.deepcopy(self.result))):
            return self.wait(self.post('/ui/path/start', request={})['task'])

    def test_peek_read_only_validates_anchors_and_preserves_literals(self):
        before = {p.name: p.read_bytes() for p in self.ws.root.glob('*.json')}
        data = research_ui.get(self.app, 'ui/peek/test001', {'scope': self.scope})
        self.assertEqual(data['records'][0]['anchor'], 'body')
        self.assertEqual(data['questions'][0]['anchor'], '')
        self.assertIn('<script>', data['records'][0]['body'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.ws.root.glob('*.json')})

    def test_get_wrong_scope_is_rejected(self):
        with self.assertRaises(ValueError): research_ui.get(self.app, 'ui/path', {'scope': 'other'})

    def test_post_missing_scope_is_rejected(self):
        with self.assertRaises(ValueError): research_ui.post(self.app, '/ui/path/start', {})

    def test_citations_only_from_source_evidence(self):
        steps = copy.deepcopy(self.result['results'])
        self.assertEqual(research_ui.relations(steps), [{'source': 'openalex:W1', 'target': 'openalex:W2', 'evidence': 'referenced_works'}])
        steps[0]['paper'].pop('referenced_works')
        self.assertEqual(research_ui.relations(steps), [])

    def test_background_result_persists_without_touching_paper(self):
        before = self.ws.paper_path.read_bytes()
        task = self.start()
        self.assertEqual(task['state'], 'completed')
        self.assertEqual(research_ui.latest(self.lib.root)['results'], self.result['results'])
        self.assertEqual(before, self.ws.paper_path.read_bytes())
        self.assertEqual(research_ui.get(self.app, 'ui/path', {})['revision'], task['result']['revision'])

    def test_reorder_persists_with_new_revision(self):
        self.start(); old = research_ui.latest(self.lib.root)
        new = self.post('/ui/path/reorder', revision=old['revision'], keys=['openalex:W2', 'openalex:W1'])
        self.assertNotEqual(new['revision'], old['revision'])
        self.assertEqual(research_ui.latest(self.lib.root)['results'][0]['paper_key'], 'openalex:W2')

    def test_stale_revision_reorder_rejected(self):
        self.start()
        with self.assertRaises(ValueError): self.post('/ui/path/reorder', revision='old', keys=[])

    def test_reorder_cannot_drop_or_duplicate_nodes(self):
        self.start(); path = research_ui.latest(self.lib.root)
        with self.assertRaises(ValueError): self.post('/ui/path/reorder', revision=path['revision'], keys=['openalex:W1', 'openalex:W1'])

    def test_cancelled_mock_response_never_saves(self):
        def effect(*args, **kwargs):
            kwargs['_cancel'].set()
            return copy.deepcopy(self.result)
        self.assertEqual(self.start(effect)['state'], 'cancelled')
        self.assertEqual(research_ui.latest(self.lib.root)['results'], [])

    def test_pdf_changed_during_response_rejected(self):
        def effect(*args, **kwargs):
            self.ws.source_pdf.write_bytes(b'changed'); return copy.deepcopy(self.result)
        self.assertEqual(self.start(effect)['state'], 'failed')
        self.assertEqual(research_ui.latest(self.lib.root)['results'], [])

    def test_manual_links_changed_during_response_rejected(self):
        def effect(*args, **kwargs):
            (self.ws.root/'manual_table_links.json').write_text('{}'); return copy.deepcopy(self.result)
        self.assertEqual(self.start(effect)['state'], 'failed')

    def test_notes_changed_during_response_rejected(self):
        def effect(*args, **kwargs):
            self.ws.apply_reader_ops([{'op':'note','note':{'id':'new','body':'Keep new note'}}])
            return copy.deepcopy(self.result)
        self.assertEqual(self.start(effect)['state'], 'failed')
        self.assertIn('new', self.ws.load('reader')['notes'])

    def test_newer_path_not_overwritten_by_old_worker(self):
        def effect(*args, **kwargs):
            planning_store.cache_put(self.lib.root, research_ui.PATH_KEY, {'revision':'human', 'results':[], 'relations':[]})
            return copy.deepcopy(self.result)
        self.assertEqual(self.start(effect)['state'], 'failed')
        self.assertEqual(research_ui.latest(self.lib.root)['revision'], 'human')

    def test_scopes_hide_tasks_and_cancel_other_library_rejected(self):
        task = self.start()
        with tempfile.TemporaryDirectory() as other:
            app = SimpleNamespace(lib=Library(Path(other)))
            self.assertEqual(research_ui.monitor(app.lib)['tasks'], [])
            with self.assertRaises(ValueError):
                research_ui.post(app, '/ui/cancel', {'scope':tasks.scope_id(other), 'id':task['id']})

    def test_scope_capture_does_not_follow_later_library_switch(self):
        original = self.lib.root
        with tempfile.TemporaryDirectory() as other:
            def effect(app, *args, **kwargs):
                self.app.lib = Library(Path(other))
                self.assertEqual(app.lib.root, original)
                return copy.deepcopy(self.result)
            self.assertEqual(self.start(effect)['state'], 'completed')
            self.assertEqual(research_ui.latest(Path(other))['results'], [])

    def test_model_failure_preserves_previous_result(self):
        self.start(); before = research_ui.latest(self.lib.root)
        def effect(*a, **kw): raise RuntimeError('Mock failure')
        self.assertEqual(self.start(effect)['state'], 'failed')
        self.assertEqual(research_ui.latest(self.lib.root), before)

    def test_existing_planner_passes_cancel_and_enriches_legacy_citations(self):
        from easyread.background import CancelEvent
        cancel = CancelEvent()
        supplied = {'title':'One', 'openalex_id':'W1'}
        retrieved = {**supplied, 'referenced_works':['W2']}
        with (patch.object(research_v2, '_search_works', return_value={'results':[retrieved]}),
              patch('easyread.chat_models.engine_cfg', return_value=({}, {'name':'Mock'})),
              patch('easyread.engines.for_translation', return_value={}),
              patch('easyread.engines.run', return_value='[{"id":"W1"}]') as run):
            result = research_v2.post(self.app, '/reading-path', {'topic':'Mock', 'papers':[supplied]}, _cancel=cancel)
        self.assertIs(run.call_args.args[4], cancel)
        self.assertEqual(result['results'][0]['paper']['referenced_works'], ['W2'])
        self.assertNotIn('referenced_works', supplied)

    def test_existing_planner_discards_late_cancelled_response(self):
        from easyread.background import CancelEvent
        from easyread.engines import Cancelled
        cancel = CancelEvent()
        def response(*a): cancel.set(); return '[{"id":"W1"}]'
        with (patch.object(research_v2, '_search_works', return_value={'results':[{'title':'One','openalex_id':'W1'}]}),
              patch('easyread.chat_models.engine_cfg', return_value=({}, {'name':'Mock'})),
              patch('easyread.engines.for_translation', return_value={}),
              patch('easyread.engines.run', side_effect=response)):
            with self.assertRaises(Cancelled): research_v2.post(self.app, '/reading-path', {'topic':'Mock'}, _cancel=cancel)
