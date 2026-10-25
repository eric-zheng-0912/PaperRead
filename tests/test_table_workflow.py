"""Status/action contracts on synthetic workspaces; no inference transport."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pdfplumber
from easyread import auto_tables, table_extract, table_policy, table_translate, translate_api
from easyread.store import Workspace, write_json_atomic, empty_reader
from tests.test_table_translation import make_table_pdf, mock_translation


class TableWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ws=Workspace(Path(self.tmp.name));make_table_pdf(self.ws.source_pdf)
        self.block={'id':'table-one','type':'table','num':'1','page':1,'caption_en':'Table 1: Statistics'}
        self.paper={'meta':{'pages':[{'n':1}],'page_count':1},'blocks':[self.block],'glossary':[]}
        write_json_atomic(self.ws.paper_path,self.paper);write_json_atomic(self.ws.reader_path,empty_reader())
        self.addCleanup(mock.patch.stopall)
        mock.patch('socket.socket.connect',side_effect=AssertionError('No external transport')).start()
        mock.patch('easyread.engines.run',side_effect=AssertionError('No model transport')).start()
        mock.patch('easyread.table_engines.recognize_document',return_value={'status':'unavailable','reason':'Mock legacy regression; no inference'}).start()

    def items(self):
        return auto_tables.handle(self.ws,{'action':'list'})['items']

    def seed(self):
        auto_tables.process(self.ws,{'engine':'openai'},run=mock_translation)

    def test_unprocessed_status_list_is_read_only(self):
        before=self.ws.paper_path.read_bytes();reader=self.ws.reader_path.read_bytes()
        self.assertEqual(self.items()[0]['state'],'unprocessed')
        self.assertFalse((self.ws.root/auto_tables.FILE).exists())
        self.assertEqual(self.ws.paper_path.read_bytes(),before);self.assertEqual(self.ws.reader_path.read_bytes(),reader)

    def test_recognized_and_single_table_enqueue(self):
        with pdfplumber.open(self.ws.source_pdf) as pdf,table_policy.explicit_job():
            source=table_extract.extract_page(pdf.pages[0],1).sources[0]
        self.paper['blocks']=[{**source,'id':self.block['id']}]
        write_json_atomic(self.ws.paper_path,self.paper)
        self.assertEqual(self.items()[0]['state'],'recognized')
        jobs=mock.Mock()
        translate_api.enqueue(jobs,self.ws,{'tables_only':True,'table_id':self.block['id'],'pages':'1'})
        self.assertEqual(jobs.enqueue.call_args.kwargs['table_id'],self.block['id'])
        self.assertEqual(jobs.enqueue.call_args.kwargs['pages'],[1])

    def test_valid_completed_cache_is_not_refreshed_by_opening_list(self):
        self.seed();before=(self.ws.root/auto_tables.FILE).read_bytes()
        item=self.items()[0]
        self.assertEqual(item['state'],'pending_check');self.assertTrue(item['cache_valid'])
        self.assertEqual((self.ws.root/auto_tables.FILE).read_bytes(),before)

    def test_unplaced_and_ambiguous_cache_offer_one_candidate_card(self):
        self.seed();value=auto_tables.state(self.ws)
        for d in value['drafts'].values():d.update(anchor=None,anchor_hash=None)
        write_json_atomic(self.ws.root/auto_tables.FILE,value)
        self.assertEqual(self.items()[0]['state'],'translated_unplaced')
        self.paper['blocks'].append({**self.block,'id':'other-position'})
        write_json_atomic(self.ws.paper_path,self.paper)
        items=self.items();self.assertEqual(len(items),1)
        self.assertEqual(items[0]['state'],'ambiguous');self.assertEqual(len(items[0]['candidate_block_ids']),2)
        self.assertTrue(items[0]['cache_valid'])

    def test_recognition_failure_with_no_candidate_still_has_page_and_reason(self):
        with mock.patch.object(table_extract,'extract_page',return_value=table_extract.PageTables([],[])):
            result=auto_tables.process(self.ws,{'engine':'openai'},run=lambda *a: self.fail('No translation'))
        self.assertEqual(result['pending'],1)
        item=self.items()[0];self.assertEqual(item['state'],'recognition_failed')
        self.assertEqual(item['page'],1);self.assertTrue(item['reason'])

    def test_cancelled_failed_and_processing_states_keep_source_unchanged(self):
        original=self.ws.paper_path.read_bytes()
        for state,expected in [('cancelled','cancelled'),('error','failed'),('running','processing')]:
            write_json_atomic(self.ws.root/'job.json',{'tables_only':True,'state':state,'pages':[1],'error':'synthetic reason'})
            self.assertEqual(self.items()[0]['state'],expected)
        self.assertEqual(self.ws.paper_path.read_bytes(),original)

    def test_cancelled_recent_job_keeps_valid_old_table(self):
        self.seed();write_json_atomic(self.ws.root/'job.json',{'tables_only':True,'state':'cancelled','pages':[1]})
        item=self.items()[0];self.assertEqual(item['state'],'pending_check');self.assertEqual(item['job_state'],'cancelled')

    def test_manual_edits_are_protected_and_stale_cache_is_not_advertised(self):
        reader=empty_reader();reader['edits']['table-one#caption']={'zh':'人工表題'}
        write_json_atomic(self.ws.reader_path,reader)
        self.assertEqual(self.items()[0]['state'],'manual_protected')
        self.seed();value=auto_tables.state(self.ws)
        for d in value['drafts'].values():d['pdf']='old-pdf'
        write_json_atomic(self.ws.root/auto_tables.FILE,value)
        self.assertFalse(self.items()[0]['cache_valid']);self.assertEqual(self.items()[0]['state'],'failed')

    def test_targeted_translation_does_not_process_other_table_on_same_page(self):
        with pdfplumber.open(self.ws.source_pdf) as pdf,table_policy.explicit_job():
            source=table_extract.extract_page(pdf.pages[0],1).sources[0]
        other=copy.deepcopy(source);other.update(num='2',caption_en='Table 2: Other source')
        self.paper['blocks'].append({**self.block,'id':'table-two','num':'2','caption_en':'Table 2: Other source'})
        write_json_atomic(self.ws.paper_path,self.paper)
        model=mock.Mock(side_effect=mock_translation)
        with mock.patch.object(table_extract,'extract_page',return_value=table_extract.PageTables([source,other],[])):
            result=auto_tables.process(self.ws,{'engine':'openai'},table_id='table-one',run=model)
        self.assertEqual(result['translated'],1);self.assertEqual(model.call_count,1)
        drafts=auto_tables.state(self.ws)['drafts'];self.assertEqual(len(drafts),1)
        self.assertEqual(next(iter(drafts.values()))['table']['num'],'1')

    def test_confirmed_paragraph_anchor_is_completed_not_requeued(self):
        self.seed();value=auto_tables.state(self.ws)
        paragraph={'id':'paragraph-anchor','type':'para','page':1,'en':'Synthetic surrounding paragraph'}
        self.paper['blocks']=[paragraph];write_json_atomic(self.ws.paper_path,self.paper)
        key=next(iter(value['drafts']))
        auto_tables.handle(self.ws,{'action':'confirm','confirmed':True,'revision':value['revision'],
                                    'draft_id':key,'block_id':paragraph['id'],'block_hash':table_translate._hash(paragraph)})
        item=self.items()[0];self.assertEqual(item['state'],'pending_check');self.assertTrue(item['cache_valid'])
        self.assertEqual(item['block_id'],paragraph['id'])

    def test_parser_cache_key_change_reuses_exact_existing_cells_without_model(self):
        self.seed();value=auto_tables.state(self.ws)
        value['drafts']={'prior-parser-key':next(iter(value['drafts'].values()))}
        write_json_atomic(self.ws.root/auto_tables.FILE,value)
        no_model=mock.Mock(side_effect=AssertionError('Existing cells must be reused'))
        result=auto_tables.process(self.ws,{'engine':'openai'},run=no_model)
        self.assertEqual(result['cached'],1);self.assertEqual(no_model.call_count,0)
        self.assertEqual(set(auto_tables.state(self.ws)['drafts']),{'prior-parser-key'})


if __name__=='__main__':unittest.main()
