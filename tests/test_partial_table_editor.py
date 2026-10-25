"""Partial/editor integration, existing isolated fixtures and Mock only."""
import copy,json,shutil
from pathlib import Path
from unittest import mock,TestCase
import tests.test_three_table_engines as fixtures
from easyread import auto_tables,table_engines,table_extract,table_editor,reader_data

class PartialEditorTest(TestCase):
    setUp=fixtures.ThreeEnginesTest.setUp
    process=fixtures.ThreeEnginesTest.process

    def partial(self,no_caption=False):
        self.native['cells'].pop(0)
        if no_caption:self.native['captions']=[]
        with mock.patch.object(table_extract,'extract_page',return_value=table_extract.PageTables([],[])):
            self.process()
        key,record=next(iter(table_editor.state(self.ws)['records'].items()))
        return key,record

    def test_sparse_mock_translation_cached_and_missing_stays_none(self):
        key,r=self.partial(no_caption=True);calls=self.model.call_count
        self.assertGreater(calls,0);self.assertIsNone(table_editor.view(r)['cells'][0]['text'])
        self.assertFalse(r['verified']);self.assertIsNone(r['human'])
        with mock.patch.object(table_extract,'extract_page',return_value=table_extract.PageTables([],[])):self.process()
        self.assertEqual(self.model.call_count,calls)
        items=auto_tables.handle(self.ws,{'action':'list'})['items']
        self.assertTrue(any(i.get('editor_id')==key for i in items))
        self.assertNotIn('table_editor_view',reader_data.reader_paper(self.ws)[0]['blocks'][1])
        self.assertFalse(auto_tables.state(self.ws)['drafts'])

    def test_human_edits_verify_reload_and_rerecognition_protection(self):
        key,r=self.partial();source=copy.deepcopy(r['source']);machine=copy.deepcopy(r['machine'])
        v=table_editor.view(r);v['cells'][0]['text']='人工補格';v['caption']='人工表題';v['footnotes']=['人工註解']
        saved=auto_tables.handle(self.ws,{'action':'editor_save','editor_id':key,'revision':0,'view':v})
        self.assertFalse(saved['verified'])
        verified=auto_tables.handle(self.ws,{'action':'editor_verify','editor_id':key,'revision':1,'confirmed':True})
        self.assertTrue(verified['verified'])
        with mock.patch.object(table_extract,'extract_page',return_value=table_extract.PageTables([],[])):self.process()
        r=table_editor.state(self.ws)['records'][key]
        self.assertEqual(r['source'],source);self.assertEqual(r['machine'],machine);self.assertEqual(r['human'],v)
        self.assertTrue(r['verified']);self.assertEqual(len(r['history']),2)
        auto_tables.handle(self.ws,{'action':'editor_save','editor_id':key,'revision':2,'view':v})
        self.assertFalse(table_editor.state(self.ws)['records'][key]['verified'])
        with self.assertRaises(ValueError):auto_tables.handle(self.ws,{'action':'editor_save','editor_id':key,'revision':0,'view':v})

    def test_overlap_and_value_conflicts_do_not_translate(self):
        self.native['cells'].pop(0);self.native['cells'][4]['text']='999'
        with mock.patch.object(table_extract,'extract_page',return_value=table_extract.PageTables([],[])):self.process()
        self.model.assert_not_called()
        r=next(iter(table_editor.state(self.ws)['records'].values()))
        self.assertIsNone(r['machine']);self.assertFalse(r['source']['translation_allowed'])

    def test_index_and_box_reassignment_is_not_hidden_by_matching_text(self):
        self.native['cells'].pop(0)
        a,b=self.native['cells'][0],self.native['cells'][1]
        a['bbox'],b['bbox']=b['bbox'],a['bbox'];a['text'],b['text']=b['text'],a['text']
        with mock.patch.object(table_extract,'extract_page',return_value=table_extract.PageTables([],[])):self.process()
        self.model.assert_not_called()
        r=next(iter(table_editor.state(self.ws)['records'].values()))
        self.assertEqual(r['source']['checks']['structure']['status'],'invalid')

    def test_editor_overlap_payload_rejected_and_original_retained(self):
        key,r=self.partial();v=table_editor.view(r);v['cells'].append(copy.deepcopy(v['cells'][0]))
        with self.assertRaises(ValueError):auto_tables.handle(self.ws,{'action':'editor_save','editor_id':key,'revision':0,'view':v})
        self.assertIsNone(table_editor.state(self.ws)['records'][key]['human'])

    def test_existing_jmlr_native_replay_only_partial_slots_preserved(self):
        root=Path(__file__).resolve().parents[1]/'tmp/three-engines-independent-20261024'
        if not (root/'native-result.json').exists():self.skipTest('Exposed evaluation artifacts unavailable')
        from easyread import pdfwork
        native=json.loads((root/'native-result.json').read_text(encoding='utf8'))['tables'][0]
        with pdfwork.open_pdf(root/'pedregosa11a.pdf') as pdf:
            page=pdf[3];tp=page.get_textpage()
            p=table_engines._partial(native,page,tp);tp.close();page.close()
        self.assertEqual(p['slots'][0][0]['status'],'missing');self.assertIsNone(p['slots'][0][0]['text'])
        self.assertEqual(sum(s['status']=='source_checked' for row in p['slots'] for s in row),55)
        self.assertTrue(p['translation_allowed']);self.assertEqual(p['checks']['metadata']['caption'],'missing_or_unknown')
        shutil.copyfile(root/'pedregosa11a.pdf',self.ws.source_pdf);self.native=copy.deepcopy(native)
        self.process()
        record=next(iter(table_editor.state(self.ws)['records'].values()))
        self.assertIsNone(record['machine']['cells'][0]['text'])
        self.assertEqual(sum(c['text'] is not None for c in record['machine']['cells']),55)
        self.assertFalse(record['verified']);self.assertIsNone(record.get('anchor'))
        self.assertGreater(self.model.call_count,0)

    def test_partial_unique_anchor_is_persistent_and_manual_view_survives_reload(self):
        key,r=self.partial()
        self.assertEqual(r['anchor'],'t')
        self.assertIn('table_editor_view',reader_data.reader_paper(self.ws)[0]['blocks'][1])
        v=table_editor.view(r);v['cells'][0]['text']='人工填入'
        auto_tables.handle(self.ws,{'action':'editor_save','editor_id':key,'revision':0,'view':v})
        shown=reader_data.reader_paper(self.ws)[0]['blocks'][1]['table_editor_view']
        self.assertEqual(shown['view']['cells'][0]['text'],'人工填入');self.assertFalse(shown['verified'])

    def test_partial_translation_cache_respects_model_settings(self):
        self.partial();calls=self.model.call_count
        with mock.patch.object(table_extract,'extract_page',return_value=table_extract.PageTables([],[])):
            auto_tables.process(self.ws,{'engine':'openai','model':'different-mock-model'},run=self.model)
        self.assertGreater(self.model.call_count,calls)

    def test_changed_recognition_revokes_check_without_overwriting_human(self):
        key,r=self.partial();v=table_editor.view(r);v['cells'][0]['text']='人工'
        auto_tables.handle(self.ws,{'action':'editor_save','editor_id':key,'revision':0,'view':v})
        auto_tables.handle(self.ws,{'action':'editor_verify','editor_id':key,'revision':1,'confirmed':True})
        review=copy.deepcopy(next(iter(auto_tables.state(self.ws)['reviews'].values())))
        review['partial_preview']['checks']['metadata']['caption']='unknown_changed'
        table_editor.capture(self.ws,review,r['pdf'])
        saved=table_editor.state(self.ws)['records'][key]
        self.assertEqual(saved['human'],v);self.assertFalse(saved['verified']);self.assertTrue(saved['source_changed'])
        with self.assertRaises(ValueError):auto_tables.handle(self.ws,{'action':'editor_verify','editor_id':key,'revision':2,'confirmed':True})
        latest=table_editor.public(saved)['latest_source_hash']
        checked=auto_tables.handle(self.ws,{'action':'editor_verify','editor_id':key,'revision':2,'confirmed':True,'source_hash':latest})
        self.assertTrue(checked['verified']);self.assertFalse(checked['source_changed'])

    def test_human_merged_view_roundtrip_and_missing_prevents_verified(self):
        key,r=self.partial();v=table_editor.view(r)
        with self.assertRaises(ValueError):auto_tables.handle(self.ws,{'action':'editor_verify','editor_id':key,'revision':0,'confirmed':True})
        first=v['cells'][0];first.update(text='合併',col_span=2)
        v['cells']=[c for c in v['cells'] if not(c['row']==0 and c['column']==1)]
        auto_tables.handle(self.ws,{'action':'editor_save','editor_id':key,'revision':0,'view':v})
        self.assertEqual(table_editor.state(self.ws)['records'][key]['human']['cells'][0]['col_span'],2)
