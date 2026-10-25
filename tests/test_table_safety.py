"""Shared false-accept gates; known regression documents, offline only."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pdfplumber
from pypdf import PdfWriter
from easyread import auto_tables, table_extract, table_policy
from easyread.store import Workspace, write_json_atomic, empty_reader
from scripts.evaluate_table_generalization import write_pdf


class TableSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ws=Workspace(Path(self.tmp.name));self.path=self.ws.source_pdf
        self.addCleanup(mock.patch.stopall)
        mock.patch('socket.socket.connect',side_effect=AssertionError('No network')).start()
        mock.patch('easyread.engines.run',side_effect=AssertionError('No live model')).start()
        mock.patch('easyread.table_engines.recognize_document',return_value={'status':'unavailable','reason':'Mock legacy regression; no inference'}).start()

    def extract(self):
        with pdfplumber.open(self.path) as pdf,table_policy.explicit_job():
            return table_extract.extract_page(pdf.pages[0],1)

    def test_unlabelled_continuation_is_rejected_at_shared_extraction_exit(self):
        write_pdf(self.path,continuation=False)
        result=self.extract()
        self.assertFalse(result.sources)
        self.assertIn('following page',result.unresolved[0]['table_issue'])
        with table_policy.explicit_job(),self.assertRaises(table_extract.UnreliableTable):
            table_extract.recognize(self.ws,{'page':1,'num':'1'})

    def test_independent_caption_on_next_page_does_not_reject_complete_table(self):
        write_pdf(self.path,continuation=True)
        writer=PdfWriter(clone_from=str(self.path))
        stream=writer.pages[1]['/Contents'].get_object()
        stream.set_data(stream.get_data().replace(b'Table 1: Estimates \\(continued\\)',b'Table 2: Different experiment'))
        writer.write(self.path)
        result=self.extract()
        self.assertEqual(len(result.sources),1,result.issues)

    def test_next_page_table_with_different_column_geometry_is_not_continuation(self):
        write_pdf(self.path,continuation=False)
        writer=PdfWriter(clone_from=str(self.path))
        # Another region with the same words is not enough: columns must agree.
        writer.pages[1].add_transformation((.65,0,0,1,0,0))
        writer.write(self.path)
        self.assertEqual(len(self.extract().sources),1)

    def test_numeric_prose_is_rejected_but_real_data_columns_survive(self):
        write_pdf(self.path,numeric_prose=True,ruled=False)
        result=self.extract();self.assertFalse(result.sources)
        self.assertIn('numeric prose',result.unresolved[0]['table_issue'])
        write_pdf(self.path,ruled=False)
        self.assertEqual(len(self.extract().sources),1)

    def test_rejected_sources_never_reach_translation_or_auto_link(self):
        for options in ({'numeric_prose':True,'ruled':False},{'continuation':False}):
            with self.subTest(options=options):
                write_pdf(self.path,**options)
                write_json_atomic(self.ws.paper_path,{'meta':{},'blocks':[{'id':'t','type':'table','num':'1','page':1,'caption_en':'Table 1: Estimates'}]})
                write_json_atomic(self.ws.reader_path,empty_reader())
                paths=[self.ws.paper_path,self.ws.reader_path,self.path]
                original=[p.read_bytes() for p in paths]
                model=mock.Mock(side_effect=AssertionError('Rejected source must not call model'))
                result=auto_tables.process(self.ws,{'engine':'openai'},pages=[1],run=model)
                self.assertEqual(result['translated'],0);self.assertGreater(result['pending'],0)
                model.assert_not_called()
                self.assertFalse(auto_tables.state(self.ws)['drafts'])
                self.assertEqual([p.read_bytes() for p in paths],original)


if __name__=='__main__':unittest.main()
