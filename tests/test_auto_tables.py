"""Automatic table drafts: synthetic PDF, Mock translation, network forbidden."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from easyread import auto_tables, reader_data, table_translate, translate_api, jobs as job_module
from easyread.store import Workspace, write_json_atomic
from tests.test_table_translation import make_table_pdf, mock_translation


class AutoTablesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name))
        make_table_pdf(self.ws.source_pdf)
        self.block = {'id': 'tab1', 'type': 'table', 'num': '1', 'page': 1,
                      'caption_en': 'Table 1: Statistics', 'caption_zh': '既有表題'}
        self.body = {'id': 'p1', 'type': 'para', 'page': 1, 'en': 'Body', 'zh': '既有正文'}
        self.save([self.body, self.block, {**self.body, 'id': 'p2'}])
        write_json_atomic(self.ws.reader_path, {'notes': {'n': {'body': '人工筆記'}}, 'edits': {}, 'progress': {'block': 'p2'}})
        self.model = mock.Mock(side_effect=mock_translation)
        self.addCleanup(mock.patch.stopall)
        mock.patch('socket.socket.connect', side_effect=AssertionError('No network')).start()
        mock.patch('easyread.engines.run', side_effect=AssertionError('No live model')).start()
        mock.patch('easyread.table_engines.recognize_document',return_value={'status':'unavailable','reason':'Mock legacy regression; no inference'}).start()

    def save(self, blocks):
        write_json_atomic(self.ws.paper_path, {'blocks': blocks, 'meta': {'pages': [{'n': 1}], 'page_count': 1}, 'glossary': []})

    def run_tables(self, **kwargs):
        return auto_tables.process(self.ws, {'engine': 'openai'}, run=self.model, **kwargs)

    def test_real_pdf_auto_position_cache_and_preservation(self):
        before = {f: (self.ws.root / f).read_bytes() for f in ['source.pdf', 'paper.json', 'reader.json']}
        result = self.run_tables()
        self.assertEqual(result['translated'], 1)
        self.assertEqual(result['pending'], 0)
        persisted = auto_tables.state(self.ws)
        link = next(iter(persisted['drafts'].values()))
        self.assertEqual(link['anchor'], 'tab1')
        self.assertEqual(link['anchor_hash'], table_translate._hash(self.block))
        self.assertFalse(link['confirmed'])  # No manual confirmation was submitted.
        paper, _ = reader_data.reader_paper(self.ws)
        self.assertEqual([b['id'] for b in paper['blocks']], ['p1', 'tab1', 'p2'])
        self.assertEqual(paper['blocks'][1]['rows_en'][0][1], '1.20')
        self.assertEqual(paper['blocks'][1]['rows_zh'][0][1], '1.20')
        self.assertEqual(paper['blocks'][1]['caption_zh'], '既有表題')
        self.assertEqual(len(paper['blocks'][1]['footnotes_zh']), 2)
        calls = self.model.call_count
        self.assertEqual(self.run_tables()['cached'], 1)
        self.assertEqual(self.model.call_count, calls)
        self.assertEqual(auto_tables.state(self.ws), persisted)
        for f, content in before.items():
            self.assertEqual((self.ws.root / f).read_bytes(), content)

    def test_borderless_layouts_persist_and_reload_without_confirmation(self):
        from tests.test_borderless_structure import specimen
        for font, align, columns in [('Helvetica', 'left', 3), ('Courier', 'right', 4), ('Times-Roman', 'center', 2)]:
            with self.subTest(font=font, align=align, columns=columns):
                (self.ws.root / auto_tables.FILE).unlink(missing_ok=True)
                truth = specimen(self.ws.source_pdf, font=font, align=align, columns=columns)
                self.block.update(num='4', caption_en='Table 4: Synthetic estimates')
                self.save([self.body, self.block, {**self.body, 'id':'p2'}])
                result = self.run_tables()
                self.assertEqual(result['pending'], 0)
                self.assertEqual(result['translated'], 1)
                reopened = Workspace(self.ws.root)
                draft = next(iter(auto_tables.state(reopened)['drafts'].values()))
                self.assertEqual(draft['anchor'], 'tab1')
                self.assertFalse(draft['confirmed'])
                blocks = reader_data.reader_paper(reopened)[0]['blocks']
                self.assertEqual([b['id'] for b in blocks], ['p1','tab1','p2'])
                self.assertEqual(blocks[1]['rows_en'], truth['rows'])
                calls = self.model.call_count
                self.assertEqual(self.run_tables()['cached'], 1)
                self.assertEqual(self.model.call_count, calls)

    def test_manual_edit_arriving_during_translation_prevents_auto_link(self):
        def response(*args, **kwargs):
            reader = self.ws.load('reader')
            reader['edits']['tab1#caption'] = {'zh':'Retain authored content'}
            write_json_atomic(self.ws.reader_path, reader)
            return mock_translation(*args, **kwargs)
        self.model.side_effect = response
        self.assertEqual(self.run_tables()['pending'], 1)
        self.assertIsNone(next(iter(auto_tables.state(self.ws)['drafts'].values()))['anchor'])
        self.assertNotIn('rows_zh', reader_data.reader_paper(self.ws)[0]['blocks'][1])

    def test_late_job_cannot_persist_auto_link(self):
        from easyread import background
        cancel = background.CancelEvent()
        owned = [True]
        fence = background.Fence(self.ws, cancel, owner=lambda: owned[0])
        def response(*args, **kwargs):
            owned[0] = False
            return mock_translation(*args, **kwargs)
        self.model.side_effect = response
        with background.bind(fence):
            self.assertTrue(self.run_tables(cancel=cancel)['failed'])
        self.assertFalse((self.ws.root / auto_tables.FILE).exists())

    def test_conflicting_persisted_link_is_not_overwritten(self):
        self.run_tables()
        value = auto_tables.state(self.ws)
        draft = next(iter(value['drafts'].values()))
        other = copy.deepcopy(draft)
        other['table']['rows_zh'][0][0] = 'Conflicting translation'
        other['translation_hash'] = table_translate._hash(other['table'])
        value['drafts']['conflicting-key'] = other
        write_json_atomic(self.ws.root / auto_tables.FILE, value)
        before = (self.ws.root / auto_tables.FILE).read_bytes()
        self.assertTrue(self.run_tables()['failed'])
        self.assertEqual((self.ws.root / auto_tables.FILE).read_bytes(), before)
        self.assertNotIn('rows_zh', reader_data.reader_paper(self.ws)[0]['blocks'][1])

    def test_ambiguous_anchor_needs_confirmation_without_another_model_call(self):
        self.save([self.body, self.block, {**self.block, 'id': 'tab-other'}])
        self.assertEqual(self.run_tables()['pending'], 1)
        self.assertNotIn('rows_en', reader_data.reader_paper(self.ws)[0]['blocks'][1])
        value = auto_tables.handle(self.ws, {'action': 'list'})
        key = next(iter(value['drafts']))
        target = next(t for t in value['targets'] if t['id'] == 'tab1')
        calls = self.model.call_count
        auto_tables.handle(self.ws, {'action': 'confirm', 'confirmed': True, 'revision': value['revision'],
                                    'draft_id': key, 'block_id': target['id'], 'block_hash': target['hash']})
        self.assertIn('rows_en', reader_data.reader_paper(self.ws)[0]['blocks'][1])
        self.assertEqual(self.model.call_count, calls)

    def test_missing_placeholder_confirm_inserts_after_selected_body_not_at_end(self):
        self.save([self.body, {**self.body, 'id': 'p2'}])
        self.assertEqual(self.run_tables()['pending'], 1)
        value = auto_tables.handle(self.ws, {'action': 'list'})
        target = value['targets'][0]
        auto_tables.handle(self.ws, {'action': 'confirm', 'confirmed': True, 'revision': value['revision'],
                                    'draft_id': next(iter(value['drafts'])), 'block_id': target['id'], 'block_hash': target['hash']})
        projected = reader_data.reader_paper(self.ws)[0]['blocks']
        self.assertEqual([b['type'] for b in projected], ['para', 'table', 'para'])
        self.assertEqual([b['type'] for b in self.ws.load('paper')['blocks']], ['para', 'para'])

    def test_changed_pdf_revokes_display(self):
        self.run_tables()
        make_table_pdf(self.ws.source_pdf, rows=[['Different', '2.00', '0.01']])
        self.assertNotIn('rows_en', reader_data.reader_paper(self.ws)[0]['blocks'][1])

    def test_manual_edit_requires_confirmation(self):
        reader = self.ws.load('reader')
        reader['edits']['tab1#caption'] = {'zh': '人工修改'}
        write_json_atomic(self.ws.reader_path, reader)
        self.assertEqual(self.run_tables()['pending'], 1)
        self.assertNotIn('rows_en', reader_data.reader_paper(self.ws)[0]['blocks'][1])

    def test_cancelled_or_invalid_translation_is_not_published(self):
        cancel = threading.Event()
        def response(*a, **k):
            cancel.set()
            return mock_translation(*a, **k)
        self.model.side_effect = response
        with self.assertRaises(table_translate.engines.Cancelled):
            self.run_tables(cancel=cancel)
        self.assertFalse((self.ws.root / auto_tables.FILE).exists())

    def test_changed_numbers_are_rejected_without_publishing(self):
        self.model.side_effect = lambda *a, **k: mock_translation(*a, **k).replace('⟦PR0⟧', '999')
        result = self.run_tables()
        self.assertTrue(result['failed'])
        self.assertFalse((self.ws.root / auto_tables.FILE).exists())

    def test_source_changed_while_translating_is_discarded(self):
        def response(*args, **kwargs):
            self.save([{**self.body, 'zh': '新的人工內容'}, self.block])
            return mock_translation(*args, **kwargs)
        self.model.side_effect = response
        self.assertTrue(self.run_tables()['failed'])
        self.assertFalse((self.ws.root / auto_tables.FILE).exists())

    def test_cached_translation_reanchors_after_whole_paper_translation(self):
        self.run_tables()
        calls = self.model.call_count
        self.save([self.body, {**self.block, 'id': 'new-table-id'}])
        self.assertEqual(self.run_tables()['cached'], 1)
        self.assertEqual(self.model.call_count, calls)
        self.assertIn('rows_zh', reader_data.reader_paper(self.ws)[0]['blocks'][1])

    def test_unreliable_structure_requires_manual_selection(self):
        make_table_pdf(self.ws.source_pdf, merged=True)
        result = self.run_tables()
        self.assertTrue(result['issues'])
        self.assertEqual(self.model.call_count, 0)

    def test_borderless_rejection_is_queued_not_silently_skipped(self):
        rejected = {'type': 'table', 'page': 1, 'num': '1', 'caption_en': 'Table 1: Statistics',
                    'table_issue': 'Borderless grid splits a source word'}
        result = auto_tables.table_extract.PageTables([], ['Table 1: Borderless grid splits a source word'], [rejected])
        before = {name: (self.ws.root / name).read_bytes() for name in ['paper.json', 'reader.json', 'source.pdf']}
        with mock.patch.object(auto_tables.table_extract, 'extract_page', return_value=result):
            report = self.run_tables()
        self.assertEqual(report['translated'], 0)
        self.assertEqual(report['pending'], 1)
        self.assertEqual(self.model.call_count, 0)
        review = next(iter(auto_tables.handle(self.ws, {'action': 'list'})['reviews'].values()))
        self.assertFalse(review['translation_available'])
        projected = reader_data.reader_paper(self.ws)[0]['blocks'][1]
        self.assertIn('Borderless grid splits', projected['manual_table_issue'])
        self.assertNotIn('rows_zh', projected)
        for name, data in before.items():
            self.assertEqual((self.ws.root / name).read_bytes(), data)

    def test_existing_rejection_job_is_visible_without_another_translation(self):
        write_json_atomic(self.ws.root / 'job.json', {'_baseline': {'pdf': table_translate.pdf_hash(self.ws)},
            'table_result': {'pending': 0, 'issues': ['PDF p.1: Table 1: Borderless grid splits a source word']}})
        value = auto_tables.handle(self.ws, {'action': 'list'})
        self.assertEqual(len(value['reviews']), 1)
        self.assertEqual(value['drafts'], {})
        self.assertFalse((self.ws.root / auto_tables.FILE).exists())
        self.assertEqual(self.model.call_count, 0)

    def test_old_pdf_rejection_not_presented_as_current(self):
        write_json_atomic(self.ws.root / 'job.json', {'_baseline': {'pdf': 'old-pdf'},
            'table_result': {'issues': ['PDF p.1: Table 1: Borderless grid splits a source word']}})
        self.assertFalse(auto_tables.handle(self.ws, {'action': 'list'})['reviews'])

    def test_stale_confirmation_does_not_write(self):
        self.run_tables()
        value = auto_tables.handle(self.ws, {'action': 'list'})
        with self.assertRaises(ValueError):
            auto_tables.handle(self.ws, {'action': 'confirm', 'confirmed': True, 'revision': value['revision']-1})

    def test_full_translation_choice_passes_and_survives_confirmation(self):
        jobs = mock.Mock()
        translate_api.enqueue(jobs, self.ws, {'pages': '1', 'include_tables': True})
        self.assertTrue(jobs.enqueue.call_args.kwargs['include_tables'])
        write_json_atomic(self.ws.root / 'job.json', {'state': 'confirm', 'pages': [1], 'include_tables': True})
        translate_api.enqueue(jobs, self.ws, {'confirmed': True})
        self.assertTrue(jobs.enqueue.call_args.kwargs['include_tables'])

    def test_background_table_only_entry_uses_drafts_without_retranslating_body(self):
        worker = job_module.Jobs.__new__(job_module.Jobs)
        worker._write = lambda ws, **values: write_json_atomic(ws.root / 'job.json', {**(ws.load('job') or {}), **values})
        before = self.ws.paper_path.read_bytes()
        with mock.patch.object(job_module.config, 'load', return_value={'engine': 'openai', 'page_cap': 60}), \
             mock.patch('easyread.engines.run', side_effect=mock_translation), \
             mock.patch.object(job_module.translate, 'translate_pages', side_effect=AssertionError('Body must not be translated')):
            worker._run_bulk(self.ws, {'tables_only': True}, threading.Event())
        self.assertEqual(self.ws.load('job')['state'], 'done')
        self.assertTrue((self.ws.root / auto_tables.FILE).exists())
        self.assertEqual(self.ws.paper_path.read_bytes(), before)

    def test_background_full_translation_runs_selected_table_option(self):
        paper = self.ws.load('paper')
        paper['meta']['target'] = 'zh-TW'
        write_json_atomic(self.ws.paper_path, paper)
        worker = job_module.Jobs.__new__(job_module.Jobs)
        worker._write = lambda ws, **values: write_json_atomic(ws.root / 'job.json', {**(ws.load('job') or {}), **values})
        with mock.patch.object(job_module.config, 'load', return_value={'engine': 'openai', 'page_cap': 60}), \
             mock.patch('easyread.engines.run', side_effect=mock_translation), \
             mock.patch.object(job_module.translate, 'translate_pages', return_value={}):
            worker._run_bulk(self.ws, {'translate': True, 'pages': [1], 'include_tables': True}, threading.Event())
        self.assertTrue((self.ws.root / auto_tables.FILE).exists())


if __name__ == '__main__':
    unittest.main()
