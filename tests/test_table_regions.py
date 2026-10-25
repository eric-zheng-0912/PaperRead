"""Local-region extraction: public regressions and distinct synthetic layouts.

All public documents here have been examined before; none is a new holdout.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pdfplumber
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import DecodedStreamObject, NameObject
from easyread import table_extract, table_regions, table_policy, pdfwork, auto_tables, reader_data
from easyread.store import Workspace, write_json_atomic, empty_reader
from scripts.evaluate_table_generalization import write_pdf
from tests.test_table_translation import mock_translation


class RegionalTablesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'sample.pdf'
        self.addCleanup(mock.patch.stopall)
        mock.patch('socket.socket.connect',side_effect=AssertionError('No network')).start()
        mock.patch('easyread.engines.run',side_effect=AssertionError('No live model')).start()
        mock.patch('easyread.table_engines.recognize_document',return_value={'status':'unavailable','reason':'Mock legacy regression; no inference'}).start()

    def extract(self):
        with pdfplumber.open(self.path) as pdf, table_policy.explicit_job():
            return table_extract.extract_page(pdf.pages[0],1)

    def test_caption_above_below_gap_and_nonbold_multiline(self):
        for options in ({'below':True},{'gap':55},{'multiline':True,'bold':False},{'caption':'Tab. 1: Estimates'}):
            with self.subTest(options=options):
                truth=write_pdf(self.path,**options)
                result=self.extract()
                self.assertEqual(len(result.sources),1,result.issues)
                b=result.sources[0]
                self.assertEqual(b['head_en']+b['rows_en'],truth[0]+truth[1])
                if options.get('below'):self.assertEqual(b['caption_pos'],'below')

    def test_adjacent_narrative_column_does_not_pollute_table(self):
        truth=write_pdf(self.path)
        original=PdfReader(self.path).pages[0]
        writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
        page.merge_transformed_page(original,Transformation().scale(.48,1))
        # Separate right-hand body column at exactly the same y positions.
        resources=page['/Resources'];font=next(iter(resources['/Font']))
        stream=DecodedStreamObject();stream.set_data(('\n'.join(
            f'BT {font} 10 Tf 330 {y} Td (Independent narrative column text.) Tj ET'
            for y in (695,662,632,602,572))).encode('ascii'))
        old=page.get_contents().get_data();stream.set_data(old+b'\n'+stream.get_data())
        page[NameObject('/Contents')]=writer._add_object(stream);writer.write(self.path)
        result=self.extract();self.assertEqual(len(result.sources),1,result.issues)
        self.assertEqual(result.sources[0]['rows_en'],truth[1])

    def test_unmarked_notes_merged_cells_and_prose_remain_rejected(self):
        for options in ({'footer':True},{'merged':True},{'duplicate':True},{'natural_prose':True,'ruled':False}):
            write_pdf(self.path,**options)
            self.assertFalse(self.extract().sources,options)

    def test_pdfium_per_cell_rejects_swapped_values_and_digit_permutation(self):
        write_pdf(self.path,below=True)
        source=self.extract().sources[0]
        chars=pdfwork.table_characters(self.path.read_bytes(),1)
        for mode in ('swap','permutation'):
            bad=copy.deepcopy(source)
            if mode=='swap':bad['rows_en'][0][1],bad['rows_en'][1][1]=bad['rows_en'][1][1],bad['rows_en'][0][1]
            else:bad['rows_en'][0][1]=bad['rows_en'][0][1].replace('1.25','2.15')
            with self.assertRaises(ValueError):table_regions.crosscheck(None,bad,chars)

    def test_local_structure_uses_existing_mock_auto_link_and_reload(self):
        ws=Workspace(Path(self.tmp.name)/'workspace');ws.root.mkdir()
        write_pdf(ws.source_pdf,below=True,multiline=True,bold=False)
        write_json_atomic(ws.paper_path,{'meta':{},'glossary':[],'blocks':[
            {'id':'before','type':'para','page':1,'en':'Before'},
            {'id':'t','type':'table','page':1,'num':'1','caption_en':'Table 1: Estimates'},
            {'id':'after','type':'para','page':1,'en':'After'}]})
        write_json_atomic(ws.reader_path,empty_reader())
        before={p:p.read_bytes() for p in (ws.paper_path,ws.reader_path,ws.source_pdf)}
        model=mock.Mock(side_effect=mock_translation)
        result=auto_tables.process(ws,{'engine':'openai'},run=model)
        self.assertEqual(result['pending'],0,result)
        self.assertEqual(result['translated'],1)
        blocks=reader_data.reader_paper(Workspace(ws.root))[0]['blocks']
        self.assertEqual([b['id'] for b in blocks],['before','t','after'])
        self.assertIn('rows_zh',blocks[1]);calls=model.call_count
        self.assertEqual(auto_tables.process(ws,{'engine':'openai'},run=model)['cached'],1)
        self.assertEqual(model.call_count,calls)
        for path,data in before.items():self.assertEqual(path.read_bytes(),data)

    def test_public_cell_truth_and_rejections(self):
        from tests.test_borderless_structure import extract
        root=Path(__file__).parent/'fixtures/tables'
        for i in (1,2,3):
            truth=json.loads((root/f'benchmark-v1/truth/nist-table-{i}.json').read_text())
            result=extract(table_extract,root/'nist-tn-2113-pages-17-19.pdf',i)
            if i!=2:self.assertFalse(result.sources);continue
            target=truth['tables'][0]
            expected=[['']*target['column_count'] for _ in range(target['row_count'])]
            for cell in target['cells']:expected[cell['row']][cell['col']]=cell['text']
            self.assertEqual(result.sources[0]['head_en']+result.sources[0]['rows_en'],expected)
            self.assertTrue(result.sources[0]['table_evidence']['pdfium_cells_verified'])


if __name__=='__main__':unittest.main()
