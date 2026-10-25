"""Synthetic workspaces only; corruption must never become empty saved data."""
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from easyread import background, store, trash
from easyread.jobs import Jobs
from easyread.library import Library


class StopWorker(Exception):
    pass


class DataIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lib = Library(Path(self.tmp.name))
        self.good = self.paper('good0001')
        self.bad = self.paper('bad00001')

    def paper(self, pid):
        root = self.lib.root / pid
        root.mkdir()
        store.write_json_atomic(root / 'paper.json', {
            'meta': {'title_en': pid, 'pages': []}, 'blocks': [],
            'translation': {'done_pages': [1]},
        })
        return store.Workspace(root)

    def test_broken_parts_stay_visible_without_blocking_good_paper(self):
        for part in ('paper', 'reader', 'item', 'discussion', 'job'):
            for raw in (b'{broken', b'\xff', b'[]', b'null'):
                with self.subTest(part=part, raw=raw):
                    path = self.bad.root / (part + '.json')
                    old = path.read_bytes() if path.exists() else None
                    path.write_bytes(raw)
                    items = {i['id']: i for i in self.lib.list()}
                    self.assertIn('load_error', items[self.bad.id])
                    self.assertNotIn('load_error', items[self.good.id])
                    self.assertEqual(path.read_bytes(), raw)
                    if old is None:
                        path.unlink()  # This test's synthetic fixture only.
                    else:
                        path.write_bytes(old)

    def test_read_error_has_path_and_update_does_not_call_mutator(self):
        self.bad.reader_path.write_bytes(b'{broken')
        with patch.object(store, 'apply_ops') as apply:
            with self.assertRaises(store.DataReadError) as caught:
                self.bad.apply_reader_ops([{'op': 'progress'}])
        self.assertEqual(caught.exception.path, self.bad.reader_path)
        apply.assert_not_called()
        self.assertEqual(self.bad.reader_path.read_bytes(), b'{broken')
        self.assertFalse(self.bad.journal_path.exists())

    def test_missing_optional_parts_keep_defaults(self):
        self.assertEqual(self.good.load('reader'), store.empty_reader())
        self.assertEqual(self.good.load('job'), {})

    def test_invalid_known_containers_do_not_block_other_papers(self):
        for part, data in (('paper', {'meta': []}), ('paper', {'blocks': [None]}),
                           ('reader', {'notes': []}), ('reader', {'notes': {'n': None}}),
                           ('discussion', {'entries': [None]}), ('item', {'meta_override': []})):
            with self.subTest(part=part, data=data):
                path = self.bad.root / (part + '.json')
                old = path.read_bytes() if path.exists() else None
                store.write_json_atomic(path, data)
                raw = path.read_bytes()
                entries = {i['id']: i for i in self.lib.list()}
                self.assertIn('load_error', entries[self.bad.id])
                self.assertNotIn('load_error', entries[self.good.id])
                self.assertEqual(path.read_bytes(), raw)
                if old is None:
                    path.unlink()
                else:
                    path.write_bytes(old)

    def test_io_error_is_reported_without_falling_back_to_defaults(self):
        with patch('easyread.store.read_json', side_effect=PermissionError('locked')):
            with self.assertRaises(store.DataReadError):
                self.bad.load('reader')

    def test_listing_is_read_only_even_with_legacy_done_pages(self):
        before = self.good.paper_path.read_bytes()
        with patch('easyread.library.page_check.repair_done') as repair:
            self.lib.list()
        repair.assert_not_called()
        self.assertEqual(self.good.paper_path.read_bytes(), before)

    def test_bad_reader_http_error_precedes_all_lazy_writes(self):
        from easyread import server
        self.bad.reader_path.write_bytes(b'{broken')
        handler = object.__new__(server.Handler)
        handler.path = f'/api/p/{self.bad.id}/state'
        handler.command = 'GET'
        handler.app = SimpleNamespace(lib=self.lib, token='mock', location=SimpleNamespace(
            status='idle', request=lambda *_: nullcontext()))
        with patch.object(server.research_api, 'get', return_value=None), \
                patch.object(server.research_v2, 'get', return_value=None), \
                patch.object(server.library_api, 'get', return_value=None), \
                patch.object(server, '_warm') as warm, \
                patch.object(server, '_refresh_layout') as refresh, \
                patch.object(handler, '_json') as response:
            handler.do_GET()
        self.assertEqual(response.call_args.args[0], 409)
        self.assertIn('preserved', response.call_args.args[1]['error'])
        warm.assert_not_called()
        refresh.assert_not_called()
        self.assertFalse(self.bad.item_path.exists())
        self.assertEqual(self.bad.reader_path.read_bytes(), b'{broken')

    def test_open_does_not_refresh_historical_annotations(self):
        from easyread import server
        reader = store.empty_reader()
        reader['ai_annotations'] = {'old': {'annotation_id': 'old', 'quote': '歷史原文'}}
        store.write_json_atomic(self.good.reader_path, reader)
        before = self.good.reader_path.read_bytes()
        handler = object.__new__(server.Handler)
        handler.path = f'/api/p/{self.good.id}/state'
        handler.app = SimpleNamespace(lib=self.lib, token='mock', location=SimpleNamespace(status='idle'))
        with patch.object(server.research_api, 'get', return_value=None), \
                patch.object(server.research_v2, 'get', return_value=None), \
                patch.object(server.library_api, 'get', return_value=None), \
                patch.object(server, '_warm'), patch.object(server, '_refresh_layout'), \
                patch('easyread.smart_annotations.refresh_after_translation') as refresh, \
                patch.object(handler, '_json'):
            handler._get()
        refresh.assert_not_called()
        self.assertEqual(self.good.reader_path.read_bytes(), before)
        self.assertFalse((self.good.root / 'history').exists())

    def test_startup_skips_corrupt_job_and_other_corrupt_parts(self):
        store.write_json_atomic(self.good.root / 'job.json', {'state': 'queued', 'run_id':'safe', '_baseline':background.snapshot(self.good)})
        (self.bad.root / 'job.json').write_bytes(b'{broken')
        with patch('easyread.jobs.threading.Thread.start'):
            jobs = Jobs(self.lib)
        self.assertEqual(jobs.bulk.get_nowait(), self.good.id)
        self.assertTrue(jobs.bulk.empty())
        self.assertEqual((self.bad.root / 'job.json').read_bytes(), b'{broken')
        store.write_json_atomic(self.bad.root / 'job.json', {'state': 'queued'})
        self.bad.reader_path.write_bytes(b'\xff')
        with patch('easyread.jobs.threading.Thread.start'):
            jobs = Jobs(self.lib)
        self.assertEqual(jobs.bulk.get_nowait(), self.good.id)
        self.assertTrue(jobs.bulk.empty())
        self.assertEqual(self.bad.load('job')['state'], 'queued')

    def test_worker_survives_job_corrupted_after_startup(self):
        with patch('easyread.jobs.threading.Thread.start'):
            jobs = Jobs(self.lib)
        (self.bad.root / 'job.json').write_bytes(b'{broken')
        store.write_json_atomic(self.good.root / 'job.json', {'state': 'queued', 'run_id':'safe', '_baseline':background.snapshot(self.good)})
        jobs.bulk.put(self.bad.id)
        jobs.bulk.put(self.good.id)
        get = jobs.bulk.get
        def next_job():
            if jobs.bulk.empty():
                raise StopWorker()
            return get()
        with patch.object(jobs.bulk, 'get', side_effect=next_job), \
                patch.object(jobs, '_run_bulk') as run, self.assertRaises(StopWorker):
            jobs._bulk_loop()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0].id, self.good.id)
        self.assertEqual((self.bad.root / 'job.json').read_bytes(), b'{broken')

    def test_worker_survives_job_corruption_during_work(self):
        with patch('easyread.jobs.threading.Thread.start'):
            jobs = Jobs(self.lib)
        for ws in (self.bad, self.good):
            store.write_json_atomic(ws.root / 'job.json', {'state': 'queued'})
            jobs.bulk.put(ws.id)
        get = jobs.bulk.get
        def next_job():
            if jobs.bulk.empty():
                raise StopWorker()
            return get()
        def work(ws, *_):
            if ws.id == self.bad.id:
                (ws.root / 'job.json').write_bytes(b'{broken')
                ws.update('job', lambda j: j.update(state='done'))
        with patch.object(jobs.bulk, 'get', side_effect=next_job), \
                patch.object(jobs, '_run_bulk', side_effect=work) as run, self.assertRaises(StopWorker):
            jobs._bulk_loop()
        self.assertEqual(run.call_count, 2)
        self.assertEqual((self.bad.root / 'job.json').read_bytes(), b'{broken')

    def test_trash_bad_json_and_utf8_do_not_block_listing(self):
        for index, raw in enumerate((b'{broken', b'\xff', b'[]')):
            folder = self.lib.root / '.trash' / (f'{index:012x}-20261022010101')
            folder.mkdir(parents=True)
            (folder / 'paper.json').write_bytes(raw)
        entries = trash.items(self.lib.root)
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(i.get('load_error') for i in entries))
        for index, raw in enumerate((b'{broken', b'\xff', b'[]')):
            self.assertEqual((self.lib.root / '.trash' / (f'{index:012x}-20261022010101') / 'paper.json').read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
