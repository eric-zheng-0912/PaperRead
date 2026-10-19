"""Isolated source-region translation acceptance; all inference is mocked."""
import copy
import json
from pathlib import Path
import threading
import os
import subprocess
import sys
import unittest
from unittest import mock

from easyread import engines, reader_data, simple_tables, table_extract
from easyread.store import Workspace
from tests.test_table_reading_translation import response
from tests import test_table_stability as isolation
from PIL import Image


class SimpleTablesTest(unittest.TestCase):
    setUp = isolation.IsolatedTableStabilityTest.setUp
    imported = isolation.IsolatedTableStabilityTest.imported
    legacy = isolation.IsolatedTableStabilityTest.legacy
    protected = isolation.IsolatedTableStabilityTest.protected
    assert_retained = isolation.IsolatedTableStabilityTest.assert_retained

    def select(self, ws, pages=(1,)):
        return [simple_tables.candidates(ws, page)[0] for page in pages]

    def translate(self, ws, regions, run=response, **kw):
        source = simple_tables.extract(ws, regions)
        return simple_tables.translate(ws, self.cfg, regions, source['source_hash'], run=run, **kw)

    def test_independent_entry_accepts_borderless_concept_merged_and_numeric_free_text(self):
        for name in ('borderless_concept', 'borderless_multiline', 'merged_header', 'ruled_concept', 'missing_caption'):
            with self.subTest(name=name):
                ws = self.imported(name)
                with mock.patch.object(table_extract, '_matrix', side_effect=AssertionError('No matrix')):
                    regions = self.select(ws)
                    preview = simple_tables.preview(ws, regions)
                    self.assertTrue(preview['source']['segments'])
                    self.assertTrue(preview['previews'][0].startswith('data:image/webp;base64,'))
                    result = self.translate(ws, regions)
                    saved = simple_tables.saved(ws)
                self.assertFalse(result['cached'])
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0]['source']['coverage'], 'selected_regions_only')
                self.assertEqual(len(saved[0]['source']['segments']), len(saved[0]['table_reading_zh']))

    def test_crosspage_append_translates_all_selected_lines_without_claiming_completeness(self):
        ws = self.imported('continued_unlabelled')
        regions = self.select(ws, (1, 2))
        self.translate(ws, regions)
        record = simple_tables.saved(ws)[0]
        text = '\n'.join(s['en'] for s in record['source']['segments'])
        for word in ('Alpha', 'Gamma', 'Delta', 'Epsilon', 'Zeta'):
            self.assertIn(word, text)
        self.assertEqual({s['page'] for s in record['source']['segments']}, {1, 2})
        self.assertEqual(record['source']['coverage'], 'selected_regions_only')

    def test_cache_is_independent_and_legacy_paper_reader_settings_pdf_unchanged(self):
        ws = self.legacy()
        original = copy.deepcopy(ws.load('paper'))
        protected = self.protected(ws)
        regions = self.select(ws)
        runner = mock.Mock(side_effect=response)
        self.assertFalse(self.translate(ws, regions, runner)['cached'])
        self.assertTrue(self.translate(ws, regions, runner)['cached'])
        self.assertEqual(runner.call_count, 1)
        self.cfg['model'] = 'changed'
        self.assertFalse(self.translate(ws, regions, runner)['cached'])
        self.assertEqual(runner.call_count, 2)
        self.assert_retained(ws, protected)
        current = ws.load('paper')
        current.pop(simple_tables.FIELD)
        self.assertEqual(current, original)
        reopened = Workspace(ws.root)
        self.assertEqual(len(simple_tables.saved(reopened)), 1)
        projected, _ = reader_data.reader_paper(reopened)
        self.assertNotIn(simple_tables.FIELD, projected)

    def test_invalid_empty_clipped_overlapping_and_stale_preview_never_translate(self):
        ws = self.imported()
        runner = mock.Mock(side_effect=AssertionError('No model'))
        for regions in ([], [{'page':True,'box':[0,0,1,1]}], [{'page':1,'box':[0,0,1,float('nan')]}],
                        [{'page':1,'box':[0,0,.01,.01]}], [{'page':1,'box':[.1,.15,.5,.25]}],
                        self.select(ws)*2):
            with self.subTest(regions=regions), self.assertRaises(ValueError):
                self.translate(ws, regions, runner)
        with self.assertRaises(ValueError):
            simple_tables.translate(ws,self.cfg,self.select(ws),'stale',run=runner)
        runner.assert_not_called()
        self.assertNotIn(simple_tables.FIELD, ws.load('paper'))

    def test_bad_model_cancel_and_pdf_change_preserve_previous_translation(self):
        ws = self.imported()
        regions = self.select(ws)
        self.translate(ws, regions)
        original = ws.paper_path.read_bytes()
        self.cfg['model'] = 'new'
        def bad(*args):
            data = json.loads(response(*args)); data['segments'][0]['zh'] += ' 999'
            return json.dumps(data)
        with self.assertRaises(ValueError):
            self.translate(ws, regions, bad)
        self.assertEqual(ws.paper_path.read_bytes(), original)
        cancel = threading.Event()
        def cancelled(*args):
            cancel.set(); return response(*args)
        with self.assertRaises(engines.Cancelled):
            self.translate(ws,regions,cancelled,cancel=cancel)
        self.assertEqual(ws.paper_path.read_bytes(), original)
        def changed(*args):
            ws.source_pdf.write_bytes(ws.source_pdf.read_bytes()+b'\n')
            return response(*args)
        with self.assertRaises(ValueError):
            self.translate(ws, regions, changed)
        self.assertEqual(ws.paper_path.read_bytes(), original)
        self.assertEqual(simple_tables.saved(ws), [])

    def test_tampered_evidence_or_translation_hidden(self):
        ws = self.imported()
        self.translate(ws,self.select(ws))
        original = copy.deepcopy(ws.load('paper'))
        for field in ('source', 'table_reading_zh'):
            def mutate(p):
                p.update(copy.deepcopy(original))
                record=p[simple_tables.FIELD][0]
                if field=='source': record['source']['segments'][0]['en']='invented'
                else: record[field][0]['zh']+='999'
            ws.update('paper',mutate)
            self.assertEqual(simple_tables.saved(ws), [])

    def test_symbols_and_numeric_only_segments_preserve_original_notation(self):
        data = (Path(__file__).parent/'fixtures/tables/provenance-symbols.pdf').read_bytes()
        ws, _ = self.lib.create_from_pdf(data,'symbols.pdf')
        regions = [{'page':1,'box':[0,0,1,1]}]
        source = simple_tables.extract(ws,regions)
        self.translate(ws,regions)
        record = simple_tables.saved(ws)[0]
        from easyread import tables
        for en, zh in zip(source['source']['segments'],record['table_reading_zh']):
            self.assertEqual(tables.protected(en['en']),tables.protected(zh['zh']))

    def test_scanned_region_safely_refuses_without_calling_model(self):
        import io
        out = io.BytesIO()
        image = Image.new('RGB',(200,150),'white')
        image.save(out,format='PDF')
        ws, _ = self.lib.create_from_pdf(out.getvalue(),'scan.pdf')
        runner = mock.Mock(side_effect=AssertionError('No OCR/model fallback'))
        with self.assertRaisesRegex(ValueError,'沒有可擷取文字'):
            self.translate(ws,[{'page':1,'box':[0,0,1,1]}],runner)
        runner.assert_not_called()
        self.assertNotIn(simple_tables.FIELD,ws.load('paper'))

    def test_other_paper_and_job_scope_are_preserved(self):
        ws = self.imported()
        other = self.imported('ruled_concept')
        original = other.paper_path.read_bytes()
        jobs = mock.Mock()
        jobs.small_status.return_value=[{'id':'foreign','kind':'simple-table','pid':other.id},
                                       {'id':'other-kind','kind':'answer','pid':ws.id}]
        # Real Jobs.small_status filters by pid; retain that contract in this fake.
        jobs.small_status.side_effect=lambda pid: [j for j in jobs.small_status.return_value if j['pid']==pid]
        self.assertFalse(simple_tables.handle(jobs,ws,{'action':'cancel','job':'foreign'})['cancelled'])
        self.assertFalse(simple_tables.handle(jobs,ws,{'action':'cancel','job':'other-kind'})['cancelled'])
        jobs.cancel_small.assert_not_called()
        self.translate(ws,self.select(ws))
        self.assertEqual(other.paper_path.read_bytes(),original)

    def test_default_exact_off_and_simple_translation_independent(self):
        from easyread import table_policy
        with mock.patch.dict(os.environ,{},clear=True):
            self.assertFalse(table_policy.enabled())
        ws = self.imported('borderless_concept')
        with mock.patch.dict(os.environ,{'PAPERREAD_DISABLE_TABLE_EXTRACTION':'1'}):
            self.assertFalse(table_policy.enabled())
            self.translate(ws,self.select(ws))
            self.assertEqual(len(simple_tables.saved(ws)),1)

    def test_desktop_mock_whole_text_preflight_keeps_real_guard_closed(self):
        # A separate process contains the packaging-only hook's monkeypatches.
        (self.root/'.simple-table-mock').write_text('Mock-only',encoding='utf8')
        ws = self.imported()
        ws.update('paper',lambda p:p.update(meta={'target':'zh-TW','page_count':2,'pages':[{'n':1},{'n':2}]},
            blocks=[{'id':'delivery-body','type':'para','page':2,
                'en':'The results of the regression in Equation 2 are provided in Table 3.'}],
            translation={'done_pages':[1,2],'en_pages':[2]}))
        code = r'''
import os, runpy, sys, threading
from pathlib import Path
from easyread import engines, translate
from easyread.store import Workspace
original_guard = engines.require_model_access
sys.frozen = True  # exercise the packaging hook inside this disposable process
hook = runpy.run_path('scripts/table_qa/desktop_mock_hook.py')
ws = Workspace(Path(sys.argv[1]))
assert engines.require_model_access is original_guard
for stage in ('before', 'after'):
    try:
        engines.require_model_access()
    except engines.EngineError:
        pass
    else:
        raise AssertionError('Real guard opened: ' + stage)
    if stage == 'before':
        failed = translate.translate_pages(ws, {'engine':'openai','target':'zh-TW','page_check':False},
            [2], threading.Event(), lambda *a:None)
        assert not failed, failed
        assert ws.load('paper')['blocks'][0].get('zh')
for pages in ([1], [1,2]):
    try:
        translate.translate_pages(ws, {'engine':'openai'}, pages, threading.Event(), lambda *a:None)
    except AssertionError:
        pass
    else:
        raise AssertionError('Unapproved fixture accepted')
engines.run = lambda *a, **kw: 'unexpected replacement'
try:
    translate.translate_pages(ws, {'engine':'openai'}, [2], threading.Event(), lambda *a:None)
except AssertionError:
    pass
else:
    raise AssertionError('Changed Mock transport accepted')
for blocked in (engines._run, engines._popen):
    try:
        blocked()
    except AssertionError:
        pass
    else:
        raise AssertionError('Real transport opened')
'''
        result = subprocess.run([sys.executable,'-c',code,str(ws.root)],capture_output=True,
            text=True,encoding='utf8',timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
