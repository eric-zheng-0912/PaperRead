"""Offline bridge/arbitration tests. Worker/model inference is always mocked.

Saved real Docling outputs are exposed regression evidence, not new inference
or independent generalization. Only temporary synthetic workspaces are written.
"""
import copy
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import pdfplumber
from easyread import auto_tables,table_engines,table_extract,table_policy,table_translate,reader_data
from easyread import tables
from easyread.store import Workspace,write_json_atomic,empty_reader
from tests.test_table_translation import make_table_pdf,mock_translation


def native_fixture(ws):
    with pdfplumber.open(ws.source_pdf) as pdf,table_policy.explicit_job():
        page=pdf.pages[0];source=table_extract.extract_page(page,1).sources[0]
        lines=table_extract._lines(page)
        cap=next(ln for ln in lines if ln['text'].startswith('Table '))
        def bbox(box):return dict(l=box[0],t=box[1],r=box[2],b=box[3],coord_origin='TOPLEFT')
        def absolute(box):return [box[i]*(page.width if i%2==0 else page.height) for i in range(4)]
        cells=[]
        for key,loc in source['table_cell_boxes'].items():
            if key.startswith('note:'):continue
            section,r,c=key.split(':');r,c=int(r),int(c)
            text=source[section+'_en'][r][c];index=r if section=='head' else r+len(source['head_en'])
            cells.append(dict(text=text,start_row_offset_idx=index,end_row_offset_idx=index+1,
                start_col_offset_idx=c,end_col_offset_idx=c+1,row_span=1,col_span=1,
                column_header=section=='head',bbox=bbox(absolute(loc['box']))))
        captions=[{'text':source['caption_en'],'provenance':[{'page_no':1,'bbox':bbox([cap['words'][0]['x0'],cap['top'],cap['words'][-1]['x1'],cap['bottom']])}]}]
        notes=[{'text':text,'provenance':[{'page_no':1,'bbox':bbox(absolute(source['table_cell_boxes'][f'note:{i}']['box']))}]} for i,text in enumerate(source['footnotes_en'])]
        native={'id':'#/tables/0','provenance':[{'page_no':1,'bbox':bbox(absolute(source['table_source_box']))}],
            'rows':len(source['head_en'])+len(source['rows_en']),'columns':len(source['rows_en'][0]),'cells':cells,'captions':captions,'footnotes':notes}
    return native


class ThreeEnginesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ws=Workspace(Path(self.tmp.name)/'paper');self.ws.root.mkdir()
        make_table_pdf(self.ws.source_pdf)
        self.paper={'meta':{},'glossary':[],'blocks':[{'id':'before','type':'para','page':1,'en':'Before'},
            {'id':'t','type':'table','page':1,'num':'1','caption_en':'Table 1: Statistics'},
            {'id':'after','type':'para','page':1,'en':'After'}]}
        write_json_atomic(self.ws.paper_path,self.paper);write_json_atomic(self.ws.reader_path,empty_reader())
        self.native=native_fixture(self.ws)
        self.addCleanup(mock.patch.stopall)
        mock.patch('socket.socket.connect',side_effect=AssertionError('No network')).start()
        mock.patch('easyread.engines.run',side_effect=AssertionError('No real API')).start()
        self.env=mock.patch.object(table_engines,'_environment',return_value=(Path('synthetic-python'),Path('synthetic-hub'),[])).start()
        def response(executable,request,timeout,cancel):
            return {'protocol':1,'pdf':request['pdf_sha256'],'tables':[copy.deepcopy(self.native)],'metrics':{'model_calls':0}}
        self.worker=mock.patch.object(table_engines,'_invoke',side_effect=response).start()
        self.model=mock.Mock(side_effect=mock_translation)

    def process(self,**kwargs):return auto_tables.process(self.ws,{'engine':'openai'},run=self.model,**kwargs)

    def test_agreement_auto_inserts_pending_and_cache_is_idempotent(self):
        before=[p.read_bytes() for p in [self.ws.source_pdf,self.ws.paper_path,self.ws.reader_path]]
        result=self.process();self.assertEqual(result['translated'],1);self.assertEqual(result['pending'],0)
        self.assertEqual(result['awaiting_review'],1)
        table=reader_data.reader_paper(self.ws)[0]['blocks'][1]
        self.assertEqual(table['auto_table_verification'],'pending')
        self.assertEqual(table['table_evidence']['method'],'docling')
        self.assertTrue(table['table_evidence']['auxiliary_agrees'])
        calls=self.model.call_count;self.assertEqual(self.process()['cached'],1)
        self.assertEqual(self.model.call_count,calls);self.assertEqual(self.worker.call_count,1)
        self.assertEqual([p.read_bytes() for p in [self.ws.source_pdf,self.ws.paper_path,self.ws.reader_path]],before)

    def test_docling_success_with_legacy_failure_still_inserts(self):
        with mock.patch.object(table_extract,'extract_page',return_value=table_extract.PageTables([],['legacy rejected'])):
            self.assertEqual(self.process()['translated'],1)
        self.assertIn('rows_zh',reader_data.reader_paper(self.ws)[0]['blocks'][1])

    def test_same_pdf_in_second_workspace_reuses_shared_recognition(self):
        self.process()
        second=Workspace(self.ws.root.parent/'duplicate');second.root.mkdir()
        shutil.copyfile(self.ws.source_pdf,second.source_pdf)
        write_json_atomic(second.paper_path,self.paper);write_json_atomic(second.reader_path,empty_reader())
        result=auto_tables.process(second,{'engine':'openai'},run=self.model)
        self.assertTrue(result['recognition']['cache_reused'])
        self.assertEqual(self.worker.call_count,1)
        self.assertEqual([b['id'] for b in reader_data.reader_paper(second)[0]['blocks']],['before','t','after'])

    def test_engine_matrix_conflict_is_preview_only_and_never_translated(self):
        with pdfplumber.open(self.ws.source_pdf) as pdf,table_policy.explicit_job():
            legacy=table_extract.extract_page(pdf.pages[0],1)
        legacy.sources[0]['rows_en'][0][1]='999'
        with mock.patch.object(table_extract,'extract_page',return_value=legacy):result=self.process()
        self.assertEqual(result['translated'],0);self.model.assert_not_called()
        table=reader_data.reader_paper(self.ws)[0]['blocks'][1]
        self.assertNotIn('rows_zh',table);self.assertTrue(table['auto_table_risk_preview'].startswith('data:image/'))
        self.assertIn('extractor_matrix_conflict',str(table['auto_table_risks']))

    def test_numeric_or_cell_location_conflict_blocks_without_backfill(self):
        self.native['cells'][4]['text']='999'
        result=self.process();self.assertEqual(result['translated'],0);self.model.assert_not_called()
        self.assertFalse(auto_tables.state(self.ws)['drafts'])
        self.assertTrue(next(iter(auto_tables.state(self.ws)['reviews'].values()))['source_audit']['blocking'])

    def test_missing_native_slot_preserves_readonly_preview_without_translation(self):
        removed=self.native['cells'].pop(0)
        with mock.patch.object(table_extract,'extract_page',return_value=table_extract.PageTables([],[])):
            result=self.process()
        self.assertEqual(result['translated'],0);self.assertGreater(self.model.call_count,0)
        review=next(iter(auto_tables.state(self.ws)['reviews'].values()))
        preview=review['partial_preview'];self.assertTrue(preview['read_only'])
        self.assertFalse(preview['complete'])
        slot=preview['slots'][removed['start_row_offset_idx']][removed['start_col_offset_idx']]
        self.assertEqual(slot['status'],'missing');self.assertIsNone(slot['text'])
        self.assertEqual(review['table_checks']['structure']['status'],'incomplete')
        self.assertTrue(any(s['status']=='source_checked' for row in preview['slots'] for s in row))
        listing=auto_tables.handle(self.ws,{'action':'list'})
        self.assertTrue(any(i.get('partial_preview') for i in listing['items']))
        self.assertNotIn('rows_zh',reader_data.reader_paper(self.ws)[0]['blocks'][1])

    def test_partial_overlap_or_numeric_conflict_never_becomes_valid_grid(self):
        self.native['cells'].append(copy.deepcopy(self.native['cells'][0]))
        result=self.process();self.assertEqual(result['translated'],0)
        review=next(iter(auto_tables.state(self.ws)['reviews'].values()))
        self.assertEqual(review['partial_preview']['checks']['structure']['status'],'invalid')
        self.assertEqual(review['partial_preview']['slots'][0][0]['status'],'conflict')
        self.assertFalse(auto_tables.state(self.ws)['drafts'])

    def test_only_punctuation_or_whitespace_difference_keeps_original_pending_text(self):
        make_table_pdf(self.ws.source_pdf,rows=[["O'Brien",'1.20','0.045'],['Control','-0.50','0.060']])
        self.native=native_fixture(self.ws)
        self.native['cells'][3]['text']='O’Brien'
        self.process()
        table=reader_data.reader_paper(self.ws)[0]['blocks'][1]
        self.assertEqual(table['rows_en'][0][0],'O’Brien')
        self.assertEqual(table['auto_table_verification'],'pending')
        self.assertTrue(any(r['kind']=='punctuation_or_whitespace' for r in table['source_audit']['risks']))

    def test_translation_masks_standalone_percent_and_unicode_symbols(self):
        source={'type':'table','table_schema':1,'head_en':[['Label','Value']],
            'rows_en':[['Revenue ₹ and rate (%)','12.00%']], 'caption_en':'Table 1: Measurements ∫', 'footnotes_en':[]}
        translated=table_translate._translate(self.ws,{'engine':'openai'},source,{},None,None,run=self.model)
        self.assertIn('₹',translated['rows_zh'][0][0]);self.assertIn('%',translated['rows_zh'][0][0])
        self.assertIn('∫',translated['caption_zh']);self.assertEqual(translated['rows_zh'][0][1],'12.00%')
        damaged=copy.deepcopy(translated);damaged['rows_zh'][0][0]=damaged['rows_zh'][0][0].replace('%','')
        self.assertTrue(tables.translation_problem(damaged))

    def test_numeric_swap_within_cells_is_not_hidden_by_whole_table_digit_bag(self):
        a,b=self.native['cells'][4],self.native['cells'][7]
        a['text'],b['text']=b['text'],a['text']
        self.assertEqual(self.process()['translated'],0)
        self.model.assert_not_called()

    def test_source_and_translation_fingerprints_are_required_for_human_verification(self):
        self.process();v=auto_tables.state(self.ws);key=next(iter(v['drafts']))
        with self.assertRaises(ValueError):auto_tables.handle(self.ws,{'action':'verify','confirmed':True,
            'revision':v['revision'],'draft_id':key,'source_fingerprint':'stale'})
        self.assertEqual(auto_tables.state(self.ws)['drafts'][key]['verification']['status'],'pending')

    def test_unavailable_timeout_missing_model_all_fall_back(self):
        for reason in ['environment missing','Docling timed out','model missing']:
            with self.subTest(reason=reason):
                self.worker.side_effect=table_engines.WorkerUnavailable(reason)
                (self.ws.root/table_engines.CACHE).unlink(missing_ok=True)
                result=self.process();self.assertEqual(result['recognition']['status'],'unavailable')
                self.assertIn('rows_zh',reader_data.reader_paper(self.ws)[0]['blocks'][1])

    def test_human_verify_and_source_change_revoke_status(self):
        self.process();value=auto_tables.handle(self.ws,{'action':'list'});item=value['items'][0]
        self.assertEqual(item['state'],'pending_check')
        auto_tables.handle(self.ws,{'action':'verify','confirmed':True,'revision':value['revision'],
            'draft_id':item['draft_id'],'source_fingerprint':item['source_fingerprint']})
        self.assertEqual(reader_data.reader_paper(self.ws)[0]['blocks'][1]['auto_table_verification'],'verified')
        self.process();self.assertEqual(reader_data.reader_paper(self.ws)[0]['blocks'][1]['auto_table_verification'],'verified')
        make_table_pdf(self.ws.source_pdf,rows=[['New','2.00','0.01'],['Control','3.00','0.04']])
        self.assertNotIn('rows_zh',reader_data.reader_paper(self.ws)[0]['blocks'][1])
        self.native=native_fixture(self.ws)
        self.process()
        self.assertEqual(self.worker.call_count,2)
        self.assertEqual(reader_data.reader_paper(self.ws)[0]['blocks'][1]['auto_table_verification'],'pending')

    def test_manual_edit_revokes_verified_projection_and_is_not_overwritten(self):
        self.process();v=auto_tables.state(self.ws);key=next(iter(v['drafts']))
        auto_tables.handle(self.ws,{'action':'verify','confirmed':True,'revision':v['revision'],'draft_id':key,
            'source_fingerprint':v['drafts'][key]['source_fingerprint']})
        reader=self.ws.load('reader');reader['edits']['t#caption']={'zh':'Author text'}
        write_json_atomic(self.ws.reader_path,reader);before=self.ws.reader_path.read_bytes()
        self.process();self.assertEqual(self.ws.reader_path.read_bytes(),before)
        self.assertNotIn('rows_zh',reader_data.reader_paper(self.ws)[0]['blocks'][1])

    def test_cancellation_and_late_source_change_do_not_publish(self):
        cancel=threading.Event()
        def late(*args):cancel.set();return {'protocol':1,'pdf':table_translate.pdf_hash(self.ws),'tables':[self.native]}
        self.worker.side_effect=late
        with self.assertRaises(table_translate.engines.Cancelled):self.process(cancel=cancel)
        self.assertFalse((self.ws.root/table_engines.CACHE).exists())
        self.assertFalse((self.ws.root/auto_tables.FILE).exists())

    def test_real_saved_docling_outputs_use_native_text_without_punctuation_repair(self):
        root=Path(__file__).resolve().parents[1];poc=root/'tmp/docling-offline-poc-20261024'
        if not (poc/'comparison.json').exists():self.skipTest('Exposed PoC native outputs not present')
        expected={('acl','1'):(5,4),('acl','2'):(11,7),('pmlr','1'):(20,9)}
        for name in ['acl','pmlr']:
            with tempfile.TemporaryDirectory() as tmp:
                ws=Workspace(Path(tmp));shutil.copyfile(root/f'tmp/independent-tables-20261024/{name}.pdf',ws.source_pdf)
                native=json.loads((poc/f'{name}-tables.json').read_text(encoding='utf8'))
                groups=table_engines.prepare_pages(ws,{'status':'ok','tables':native['tables']})
                for page,report in groups.items():
                    for source in report.sources:
                        key=(name,source['num'])
                        if key not in expected:continue
                        self.assertEqual((len(source['head_en'])+len(source['rows_en']),len(source['rows_en'][0])),expected[key])
                        self.assertFalse(source['source_audit']['blocking'])
                        if key==('acl','1'):self.assertIn("Raven's",str(source['rows_en']))
                        expected.pop(key)
        self.assertFalse(expected)

    def test_saved_real_tables_connect_to_mock_translation_and_original_anchors(self):
        root=Path(__file__).resolve().parents[1];poc=root/'tmp/docling-offline-poc-20261024'
        if not (poc/'comparison.json').exists():self.skipTest('Exposed native PoC data not present')
        for name,pages,numbers in [('acl',[3,6],['1','2']),('pmlr',[6],['1'])]:
            with tempfile.TemporaryDirectory() as tmp:
                ws=Workspace(Path(tmp)/'paper');ws.root.mkdir()
                shutil.copyfile(root/f'tmp/independent-tables-20261024/{name}.pdf',ws.source_pdf)
                blocks=[{'id':'before','type':'para','page':pages[0],'en':'Before'}]
                blocks += [{'id':'table-'+n,'type':'table','num':n,'page':page,'caption_en':'Table '+n+': source'} for n,page in zip(numbers,pages)]
                blocks += [{'id':'after','type':'para','page':pages[-1],'en':'After'}]
                write_json_atomic(ws.paper_path,{'meta':{},'glossary':[],'blocks':blocks})
                write_json_atomic(ws.reader_path,empty_reader())
                originals=[p.read_bytes() for p in [ws.source_pdf,ws.paper_path,ws.reader_path]]
                native=json.loads((poc/f'{name}-tables.json').read_text(encoding='utf8'))
                def replay(executable,request,timeout,cancel):return {'protocol':1,'pdf':request['pdf_sha256'],'tables':native['tables'],'metrics':{'model_calls':0}}
                self.worker.side_effect=replay
                result=auto_tables.process(ws,{'engine':'openai'},pages=pages,run=self.model)
                self.assertEqual(result['translated'],len(numbers),result)
                projected=reader_data.reader_paper(ws)[0]['blocks']
                self.assertEqual([b['id'] for b in projected],[b['id'] for b in blocks])
                for b in projected[1:-1]:
                    self.assertIn('rows_zh',b);self.assertEqual(b['auto_table_verification'],'pending')
                    self.assertFalse(b['source_audit']['blocking'])
                self.assertEqual([p.read_bytes() for p in [ws.source_pdf,ws.paper_path,ws.reader_path]],originals)


class WorkerLifetimeTest(unittest.TestCase):
    def test_timeout_kills_and_reaps_exactly_one_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            process=mock.Mock();alive=[True]
            process.stdin=io.BytesIO();process.poll.side_effect=lambda:None if alive[0] else 1
            process.kill.side_effect=lambda:alive.__setitem__(0,False)
            request={'runtime':tmp,'output':str(Path(tmp)/'out.json')}
            with mock.patch.object(table_engines.subprocess,'Popen',return_value=process) as popen:
                with self.assertRaises(table_engines.WorkerUnavailable):table_engines._invoke(Path('fake'),request,-1,None)
            self.assertEqual(popen.call_count,1);process.kill.assert_called_once();process.wait.assert_called_once()


if __name__=='__main__':unittest.main()
