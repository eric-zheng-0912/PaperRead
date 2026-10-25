"""Document-grouped synthetic PDFs, exact authored truth, Mock inference only.

Helvetica/left and Courier/right/Times/center groups are now known regressions.
The historical "holdout" label is retained for reproducibility, not independence.
No production PDF or previously saved segment translation is read.
"""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

import pdfplumber
from pdfminer.fontmetrics import FONT_METRICS
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from easyread import auto_tables, reader_data, table_extract, table_policy, tables
from easyread.store import Workspace, empty_reader, write_json_atomic
from tests.test_table_translation import mock_translation


def specimen(path, *, font='Helvetica', align='left', columns=3, invalid=None, variant=0, word_collision=False):
    """Coordinates are generated from column widths/font metrics, never a real PDF."""
    head = [['Group label', *[f'Model {chr(65+i)}' for i in range(columns-1)]]]
    rows = [[label, *[value for _ in range(columns-1)]] for label,value in
            [('Treatment group','-0.123***'),('Short control [12]','1.200**'),('All observations','(0.045)'),('Reference category','0.050')]]
    rows[-1][1 if columns > 2 else 0] = ''
    if word_collision:
        for row,label in zip(rows,['Control group A','Control group B','Control group C','Macroeconomic category']):
            row[0] = label
    if columns > 2:
        rows[1][-1] = 'p < 0.01'
    note = 'Note: Standard errors in parentheses; *** p < 0.01. Source: Study [12].'
    if variant:
        head = [['Category', *[f'Sample {chr(88+i)}' for i in range(columns-1)]]]
        labels = ['Urban sample [8]', 'Rural sample', 'Adjusted estimate', 'Sensitivity check']
        for i,row in enumerate(rows):
            row[0] = labels[i]
            for j in range(1,columns):
                row[j] = ['-2.345**','[0.020, 0.080]','(0.125)','0.000'][i]
        rows[-1][1 if columns > 2 else 0] = ''
        note = 'Note: Confidence intervals in brackets; ** p < 0.05. Source: Study [8].'
    width, height = 180 + columns*150, 760 + variant*20
    x0, y0, gap, size = 36+variant*3, height-100, 26+variant, 10
    widths = [185, *[135 for _ in range(columns-1)]]
    edges=[x0]
    for w in widths: edges.append(edges[-1]+w)
    bold={'Helvetica':'Helvetica-Bold','Times-Roman':'Times-Bold','Courier':'Courier-Bold'}[font]
    writer=PdfWriter();page=writer.add_blank_page(width=width+70,height=height)
    fonts={}
    for name,face in [('F1',font),('F2',bold)]:
        fonts[NameObject('/'+name)]=writer._add_object(DictionaryObject({NameObject('/Type'):NameObject('/Font'),
            NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/'+face)}))
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject(fonts)})
    ops=[]
    def put(x,y,text,face='F1'):
        text=text.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
        ops.append(f'BT /{face} {size} Tf {x:.4f} {y:.4f} Td ({text}) Tj ET')
    put(x0,y0+34,'Table 4: Synthetic estimates','F2')
    for i,row in enumerate(head+rows):
        for j,text in enumerate(row):
            if not text: continue
            selected=bold if i==0 else font
            text_width=sum(FONT_METRICS[selected][1].get(ch,600) for ch in text)*size/1000
            x=edges[j]
            if align=='right':x=edges[j+1]-20-text_width
            elif align=='center':x=(edges[j]+edges[j+1]-text_width)/2
            y=y0-i*gap
            if invalid=='irregular' and i==3:y-=gap/2
            if invalid=='overlap' and i==2 and j==0:text+=' broad text ' * 8
            if invalid=='no-bold' and i==0:face='F1'
            else:face='F2' if i==0 else 'F1'
            put(x,y,text,face)
    if invalid=='merged-header':put(edges[1]-20,y0,'Merged heading over two columns','F2')
    if invalid=='wrapped':put(x0,y0-gap-size-1,'ambiguous continuation text')
    if invalid!='no-closing-note':put(x0,y0-(len(rows))*gap-18,note)
    else:
        # Body text aligned with preceding data is not another table row. A
        # parser accepting this extra row has genuinely crossed the boundary.
        for j,text in enumerate(['Following narrative', *['2020 evidence' for _ in range(columns-1)]]):
            put(edges[j],y0-(len(rows)+1)*gap,text)
    content=DecodedStreamObject();content.set_data('\n'.join(ops).encode('ascii'))
    page[NameObject('/Contents')]=writer._add_object(content);writer.write(path)
    # Boldness/regular spacing are conservative eligibility requirements of the
    # new path, not ground truth: valid non-bold/irregular tables count as misses.
    return {'head':head,'rows':rows,'notes':[note] if invalid!='no-closing-note' else [],
            'reliable':invalid in (None,'no-bold','irregular')}


def development_cases():
    return ([{'id':f'helvetica-left-{n}', 'group':'dev-helvetica-left','columns':n} for n in (2,3,4)] +
            [{'id':'word-edge-collision','group':'dev-helvetica-left','columns':3,'word_collision':True}] +
            [{'id':'dev-'+v,'group':'dev-helvetica-left','invalid':v} for v in
             ('no-bold','overlap','irregular','merged-header','wrapped','no-closing-note')])


def holdout_cases():
    # Previously evaluated groups, no longer an unseen holdout.
    return [{'id':f'{font}-{n}-{bad or "valid"}', 'group':font+'-'+align,'font':font,'align':align,
             'columns':n,'variant':2,'invalid':bad} for font,align in [('Courier','right'),('Times-Roman','center')]
            for n,bad in [(2,None),(4,None),(3,'overlap'),(3,'wrapped')]]


def extract(module,path,page_number=1):
    with pdfplumber.open(path) as pdf,table_policy.explicit_job():
        return module.extract_page(pdf.pages[page_number-1],page_number)


class BorderlessStructureTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'source.pdf'
        self.addCleanup(mock.patch.stopall)
        mock.patch('socket.socket.connect',side_effect=AssertionError('No network')).start()
        mock.patch('easyread.engines.run',side_effect=AssertionError('No paid models')).start()
        mock.patch('easyread.table_engines.recognize_document',return_value={'status':'unavailable','reason':'Mock legacy regression; no inference'}).start()

    def test_word_boundaries_and_sparse_cells_across_column_counts(self):
        for n in (2,3,4):
            truth=specimen(self.path,columns=n)
            result=extract(table_extract,self.path)
            self.assertFalse(result.issues, result.issues)
            self.assertEqual(len(result.sources),1)
            table=result.sources[0]
            self.assertEqual(table['head_en'],truth['head']);self.assertEqual(table['rows_en'],truth['rows'])
            self.assertEqual(table['footnotes_en'],truth['notes'])
            self.assertEqual(table['table_evidence']['method'],'text')
            self.assertEqual(table['table_structure']['columns'],n)
            self.assertEqual(len(table['table_structure']['cell_ids']),5*n)

    def test_unsupported_or_ambiguous_documents_are_refused_without_partial_cells(self):
        for bad in ('overlap','merged-header','wrapped','no-closing-note'):
            with self.subTest(bad=bad):
                specimen(self.path,invalid=bad);result=extract(table_extract,self.path)
                self.assertFalse(result.sources);self.assertTrue(result.issues)

    def test_legacy_valid_nonbold_and_irregular_rows_are_recovered(self):
        for kind in ('no-bold','irregular'):
            with self.subTest(kind=kind):
                truth=specimen(self.path,invalid=kind)
                result=extract(table_extract,self.path)
                self.assertFalse(result.issues)
                self.assertEqual(result.sources[0]['rows_en'],truth['rows'])
                self.assertEqual(result.sources[0]['table_evidence']['method'],'text')

    def test_two_valid_conflicting_matrices_are_rejected(self):
        specimen(self.path)
        original=extract(table_extract,self.path).sources[0]
        for field in ('rows_en','head_en','footnotes_en','caption_en'):
            other=copy.deepcopy(original)
            if field=='rows_en':other[field][0][1],other[field][1][1]=other[field][1][1],other[field][0][1]
            elif field=='head_en':other[field][0][0]='Different header'
            elif field=='footnotes_en':other[field]=['Different note']
            else:other[field]='Table 4: Conflicting caption'
            combined=table_extract._combine_tables(table_extract.PageTables([original],[]),table_extract.PageTables([other],[]))
            self.assertFalse(combined.sources);self.assertEqual(len(combined.unresolved),1)
            self.assertIn('conflict',combined.issues[0])

    def test_equivalent_matrices_prefer_legacy_geometry(self):
        specimen(self.path);original=extract(table_extract,self.path).sources[0]
        other=copy.deepcopy(original);other['table_source_box']=[.1,.2,.8,.7]
        combined=table_extract._combine_tables(table_extract.PageTables([original],[]),table_extract.PageTables([other],[]))
        self.assertIs(combined.sources[0],original)
        self.assertFalse(combined.issues)

    def test_recovered_legacy_tables_reach_drafts_and_original_placeholder(self):
        for kind in ('no-bold','irregular'):
            truth=specimen(self.path,invalid=kind);ws=Workspace(Path(self.tmp.name))
            write_json_atomic(ws.paper_path,{'meta':{},'blocks':[
                {'id':'t','type':'table','num':'4','page':1,'caption_en':'Table 4: Synthetic estimates'}]})
            write_json_atomic(ws.reader_path,empty_reader())
            original=ws.paper_path.read_bytes();reader=ws.reader_path.read_bytes()
            result=auto_tables.process(ws,{'engine':'openai'},run=mock_translation)
            self.assertEqual(result['translated'],1);self.assertEqual(result['pending'],0)
            projected=reader_data.reader_paper(ws)[0]['blocks'][0]
            self.assertEqual(projected['id'],'t');self.assertEqual(projected['rows_en'],truth['rows'])
            self.assertEqual(projected['rows_zh'][0][1],'-0.123***')
            self.assertTrue(projected['table_cell_boxes']);self.assertTrue(projected['table_structure'])
            self.assertEqual(ws.paper_path.read_bytes(),original);self.assertEqual(ws.reader_path.read_bytes(),reader)

    def test_new_method_is_used_only_for_failed_legacy_table(self):
        specimen(self.path);source=extract(table_extract,self.path).sources[0]
        rejected={**source,'table_issue':'Legacy word split'}
        combined=table_extract._combine_tables(table_extract.PageTables([],['Legacy word split'],[rejected]),table_extract.PageTables([source],[]))
        self.assertEqual(combined.sources,[source]);self.assertFalse(combined.unresolved)
        both=table_extract._combine_tables(table_extract.PageTables([],[],[rejected]),table_extract.PageTables([],[],[rejected]))
        self.assertFalse(both.sources);self.assertEqual(len(both.unresolved),1)

    def test_repeated_inner_word_edges_never_split_a_long_word(self):
        truth=specimen(self.path,word_collision=True)
        result=extract(table_extract,self.path)
        self.assertFalse(result.issues)
        self.assertEqual(result.sources[0]['rows_en'],truth['rows'])

    def test_mock_cell_translation_persists_structure_without_touching_old_content(self):
        truth=specimen(self.path,columns=4);ws=Workspace(Path(self.tmp.name))
        original={'meta':{'pages':[{'n':1}],'target':'zh-TW'},'glossary':[],
                  'simple_table_translations':[{'id':'old-unstructured','table_reading_zh':[{'id':'old','zh':'既有段落'}]}],
                  'blocks':[{'id':'before','type':'para','page':1,'zh':'人工正文'},
                            {'id':'tab4','type':'table','num':'4','page':1,'caption_en':'Table 4: Synthetic estimates'},
                            {'id':'after','type':'para','page':1,'zh':'後文'}]}
        write_json_atomic(ws.paper_path,original);write_json_atomic(ws.reader_path,empty_reader())
        before={name:(ws.root/name).read_bytes() for name in ('paper.json','reader.json','source.pdf')}
        model=mock.Mock(side_effect=mock_translation)
        result=auto_tables.process(ws,{'engine':'openai'},run=model)
        self.assertEqual(result['translated'],1);self.assertEqual(result['pending'],0)
        saved=next(iter(auto_tables.state(ws)['drafts'].values()))['table']
        self.assertEqual(saved['rows_en'],truth['rows']);self.assertEqual(saved['rows_zh'][-1][1],'')
        self.assertEqual(saved['rows_zh'][0][1],'-0.123***')
        self.assertEqual(tables.protected(saved['rows_en'][1][0]),tables.protected(saved['rows_zh'][1][0]))
        self.assertEqual(tables.protected(saved['footnotes_en'][0]),tables.protected(saved['footnotes_zh'][0]))
        self.assertIsNone(tables.translation_problem(saved))
        projected=reader_data.reader_paper(ws)[0]['blocks']
        self.assertEqual([b['id'] for b in projected],['before','tab4','after'])
        self.assertEqual(projected[1]['table_schema'],1)
        calls=model.call_count;self.assertEqual(auto_tables.process(ws,{'engine':'openai'},run=model)['cached'],1)
        self.assertEqual(calls,model.call_count)
        for name,data in before.items():self.assertEqual((ws.root/name).read_bytes(),data)

    def test_existing_cell_edit_requires_confirmation_and_is_preserved(self):
        specimen(self.path);ws=Workspace(Path(self.tmp.name))
        write_json_atomic(ws.paper_path,{'meta':{'pages':[{'n':1}]},'blocks':[
            {'id':'table-anchor','type':'table','page':1,'num':'4','caption_en':'Table 4: Synthetic estimates'}]})
        reader=empty_reader();reader['edits']['table-anchor#caption']={'zh':'人工表題'}
        reader['notes']['keep']={'body':'保留筆記'};reader['progress']={'block':'table-anchor','ratio':.5}
        write_json_atomic(ws.reader_path,reader)
        original=ws.reader_path.read_bytes();paper=ws.paper_path.read_bytes()
        result=auto_tables.process(ws,{'engine':'openai'},run=mock_translation)
        self.assertEqual(result['pending'],1)
        self.assertNotIn('rows_zh',reader_data.reader_paper(ws)[0]['blocks'][0])
        self.assertEqual(ws.reader_path.read_bytes(),original);self.assertEqual(ws.paper_path.read_bytes(),paper)

    def test_additive_structure_descriptor_does_not_invalidate_existing_cache(self):
        specimen(self.path);ws=Workspace(Path(self.tmp.name))
        write_json_atomic(ws.paper_path,{'meta':{},'blocks':[{'id':'t','type':'table','page':1,'num':'4','caption_en':'Table 4: Synthetic estimates'}]})
        write_json_atomic(ws.reader_path,empty_reader())
        auto_tables.process(ws,{'engine':'openai'},run=mock_translation)
        value=auto_tables.state(ws);draft=next(iter(value['drafts'].values()))
        draft['table'].pop('table_structure')
        draft['translation_hash']=auto_tables.table_translate._hash(draft['table'])
        write_json_atomic(ws.root/auto_tables.FILE,value)
        no_model=mock.Mock(side_effect=AssertionError('Cache must not pay again'))
        self.assertEqual(auto_tables.process(ws,{'engine':'openai'},run=no_model)['cached'],1)
        self.assertEqual(no_model.call_count,0)


def evaluate(baseline,split):
    spec=importlib.util.spec_from_file_location('easyread._word_gutter_baseline',baseline)
    before=importlib.util.module_from_spec(spec);sys.modules[spec.name]=before;spec.loader.exec_module(before)
    if split=='independent-public':
        return evaluate_public(before)
    cases=development_cases() if split=='development' else holdout_cases()
    report={'split':split,'evaluation_role':'known-regression',
            'document_groups':sorted({c['group'] for c in cases}),'documents':len(cases),'before':{},'after':{}}
    with tempfile.TemporaryDirectory() as tmp:
        samples=[]
        for i,case in enumerate(cases):
            path=Path(tmp)/f'document-{i}.pdf'
            args={k:v for k,v in case.items() if k not in ('id','group')}
            samples.append((path,specimen(path,**args)))
        for label,module in [('before',before),('after',table_extract)]:
            counts=dict(detected=0,accepted=0,exact=0,incorrect_acceptances=0,safe_rejections=0,notation_exact=0)
            for path,truth in samples:
                result=extract(module,path)
                counts['detected']+=bool(result.sources or result.unresolved)
                accepted=bool(result.sources);counts['accepted']+=accepted
                exact=(len(result.sources)==1 and result.sources[0]['head_en']==truth['head'] and result.sources[0]['rows_en']==truth['rows'] and result.sources[0]['footnotes_en']==truth['notes'])
                counts['exact']+=bool(exact and truth['reliable'])
                counts['incorrect_acceptances']+=bool(accepted and (not truth['reliable'] or not exact))
                counts['safe_rejections']+=bool(not accepted and not truth['reliable'])
                if accepted and len(result.sources)==1:
                    actual=result.sources[0];expected=truth['head']+truth['rows'];cells=actual['head_en']+actual['rows_en']
                    note_pairs=zip(actual['footnotes_en'],truth['notes'])
                    faithful=(len(cells)==len(expected) and all(len(a)==len(b) for a,b in zip(cells,expected)) and
                              all(tables.protected(a)==tables.protected(b) for ar,br in zip(cells,expected) for a,b in zip(ar,br)) and
                              len(actual['footnotes_en'])==len(truth['notes']) and
                              all(tables.protected(a)==tables.protected(b) for a,b in note_pairs))
                    counts['notation_exact']+=faithful
            positive=sum(t['reliable'] for _,t in samples);negative=len(samples)-positive
            counts.update(reliable_documents=positive,unreliable_documents=negative,
                exact_reconstruction_rate=counts['exact']/positive,
                incorrect_acceptance_rate=counts['incorrect_acceptances']/len(samples),
                safe_rejection_rate=counts['safe_rejections']/negative,
                notation_fidelity_among_accepted=counts['notation_exact']/counts['accepted'] if counts['accepted'] else None)
            report[label]=counts
    print(json.dumps(report,indent=2))


def evaluate_public(before):
    """Existing public PDF fixtures from three document groups, never tuned here.

    Their annotations cover cells but not complete footnotes, so this separate
    comparison makes only cell-level claims (not full-table completeness).
    """
    root=Path(__file__).parent/'fixtures/tables'
    entries=[('NIST',root/'nist-tn-2113-pages-17-19.pdf',f'nist-table-{i}') for i in (1,2,3)] + [
        ('FBI',root/'benchmark-v1/regression/fbi-nics-2015-11.pdf','fbi-nics-table'),
        ('W3C',root/'benchmark-v1/holdout/w3c-table.pdf','w3c-pdf6-table')]
    output={'split':'independent-public','evaluation_role':'known-public-regression','document_groups':['NIST','FBI','W3C'],'table_pages':5,
            'limitation':'Public annotations cover cells, not full footnotes; no full-table completeness claim'}
    for label,module in [('before',before),('after',table_extract)]:
        stats={'detected':0,'accepted':0,'exact_cell_reconstructions':0,'incorrect_acceptances':0,'safe_rejections':0,'reliable_pages':0,'unreliable_pages':0}
        for group,path,name in entries:
            truth=json.loads((root/'benchmark-v1/truth'/f'{name}.json').read_text(encoding='utf8'))
            target=truth['tables'][0];reliable=not truth['expected_fallback']
            stats['reliable_pages' if reliable else 'unreliable_pages']+=1
            result=extract(module,path,target['page'])
            stats['detected']+=bool(result.sources or result.unresolved);stats['accepted']+=bool(result.sources)
            expected=[['']*target['column_count'] for _ in range(target['row_count'])]
            for cell in target['cells']:expected[cell['row']][cell['col']]=cell['text']
            actual=result.sources[0]['head_en']+result.sources[0]['rows_en'] if len(result.sources)==1 else None
            exact=actual==expected
            stats['exact_cell_reconstructions']+=bool(reliable and exact)
            stats['incorrect_acceptances']+=bool(result.sources and (not reliable or not exact))
            stats['safe_rejections']+=bool(not result.sources and not reliable)
        output[label]=stats
    print(json.dumps(output,indent=2))


if __name__=='__main__':
    if '--evaluate' in sys.argv:
        parser=argparse.ArgumentParser();parser.add_argument('--evaluate',action='store_true');parser.add_argument('--baseline',required=True);parser.add_argument('--split',choices=['development','holdout','independent-public'],required=True)
        args=parser.parse_args();evaluate(args.baseline,args.split)
    else:unittest.main()
