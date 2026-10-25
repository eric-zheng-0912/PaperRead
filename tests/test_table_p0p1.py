"""Machine-only Traditional boundary and source-evidence review identity."""
import copy,json,hashlib,tempfile
from pathlib import Path
from unittest import TestCase
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject,DictionaryObject,NameObject
from easyread import table_translate,table_editor,auto_tables,table_review_identity
from easyread.store import Workspace,write_json_atomic
import tests.test_partial_table_editor as fixtures

class MachineBoundaryTest(TestCase):
    def test_carrier_without_type_is_normalized_without_touching_source_or_notation(self):
        original={'rows_en':[['原文 简体 12%']],'head_zh':[['测试']],
                  'rows_zh':[['翻译内容 12% ± 0.5 [3] $x_{2}=1$ `变量`']],
                  'caption_zh':'表题 1','footnotes_zh':['标准误 † 0.01']}
        before=copy.deepcopy(original);result=table_translate.normalize_machine(original)
        self.assertEqual(result['head_zh'],[['測試']]);self.assertIn('翻譯內容',result['rows_zh'][0][0])
        self.assertIn('`变量`',result['rows_zh'][0][0]);self.assertIn('$x_{2}=1$',result['rows_zh'][0][0])
        self.assertEqual(result['rows_en'],before['rows_en']);self.assertEqual(original,before)
        self.assertEqual(table_translate.tables.protected(result['rows_zh'][0][0]),table_translate.tables.protected(before['rows_zh'][0][0]))

class CacheTest(TestCase):
    setUp=fixtures.PartialEditorTest.setUp
    process=fixtures.PartialEditorTest.process
    partial=fixtures.PartialEditorTest.partial

    def test_partial_new_cache_and_reload_are_traditional_and_preserve_missing_slot(self):
        key,r=self.partial();self.assertIn('翻譯內容',str(r['machine']))
        self.assertNotIn('翻译内容',str(r['machine']));self.assertIsNone(r['machine']['cells'][0]['text'])
        calls=self.model.call_count
        source=copy.deepcopy(r['source']);state=table_editor.state(self.ws)
        state['records'][key]['machine']['caption']='测试译文';write_json_atomic(self.ws.root/table_editor.FILE,state)
        reloaded=table_editor.state(self.ws)['records'][key]
        self.assertEqual(reloaded['machine']['caption'],'測試譯文');self.assertEqual(reloaded['source'],source)
        self.assertEqual(self.model.call_count,calls)

    def test_human_and_verified_editor_records_are_untouched_even_with_simplified_text(self):
        key,r=self.partial();state=table_editor.state(self.ws);r=state['records'][key]
        r['human']=table_editor.view(r);r['human']['caption']='人工输入不转换';r['verified']=True
        r['machine']['caption']='历史机器文本';before=copy.deepcopy(r)
        write_json_atomic(self.ws.root/table_editor.FILE,state)
        self.assertEqual(table_editor.state(self.ws)['records'][key],before)

    def test_dense_cache_reload_converts_only_unverified_machine_targets(self):
        self.process();state=auto_tables.state(self.ws);key=next(iter(state['drafts']));d=state['drafts'][key]
        source=copy.deepcopy(d['table']['rows_en']);d['table']['caption_zh']='测试译文 1'
        d['translation_hash']=table_translate._hash(d['table']);write_json_atomic(self.ws.root/auto_tables.FILE,state)
        loaded=auto_tables.state(self.ws)['drafts'][key]
        self.assertEqual(loaded['table']['caption_zh'],'測試譯文 1');self.assertEqual(loaded['table']['rows_en'],source)
        d['verification']['status']='verified';write_json_atomic(self.ws.root/auto_tables.FILE,state)
        self.assertEqual(auto_tables.state(self.ws)['drafts'][key],d)

class ReviewIdentityTest(TestCase):
    def test_two_physical_tables_on_same_page_group_only_their_own_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            ws=Workspace(Path(temp));writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
            font=writer._add_object(DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')}))
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
            ops=[]
            for y,n in [(700,'1'),(400,'2')]:
                ops.extend([f'BT /F1 10 Tf 60 {y} Td (Source cells) Tj ET',f'BT /F1 10 Tf 60 {y-50} Td (Table {n}: Caption) Tj ET'])
            stream=DecodedStreamObject();stream.set_data('\n'.join(ops).encode('ascii'));page[NameObject('/Contents')]=writer._add_object(stream)
            with ws.source_pdf.open('wb') as out:writer.write(out)
            digest=hashlib.sha256(ws.source_pdf.read_bytes()).hexdigest();reviews={};items=[]
            for n,top in [('1',70),('2',370)]:
                box=[50/612,top/792,500/612,(top+50)/792]
                reviews['native'+n]={'pdf':digest,'page':1,'num':'','table_source_box':box,'table_native':{'id':'#/tables/'+n,'footnotes':[]}}
                reviews['legacy'+n]={'pdf':digest,'page':1,'num':n,'caption_en':'Table '+n+': Caption'}
                items.extend([{'id':'native'+n,'page':1,'num':'','state':'pending_check','editor_id':'editor'+n,'regions':[{'page':1,'box':box}]},
                              {'id':'legacy'+n,'block_id':'anchor'+n,'page':1,'num':n,'state':'recognition_failed'}])
            result=table_review_identity.groups(ws,items,{'reviews':reviews,'drafts':{}},digest)
            self.assertEqual(len(result),2);self.assertEqual(len({i['source_id'] for i in result}),2)
            self.assertTrue(all(len(i['evidence_members'])==2 for i in result))
            self.assertEqual({tuple(i['candidate_block_ids']) for i in result},{('anchor1',),('anchor2',)})

    def test_exposed_jmlr_state_groups_without_modifying_any_saved_record(self):
        root=Path(__file__).resolve().parents[1]/'tmp/table-ui-20261025/profile/library/partial-jmlr'
        if not root.exists():self.skipTest('Exposed UI fixture unavailable')
        ws=Workspace(root);files=[root/'table_editor.json',root/'auto_table_drafts.json',ws.source_pdf]
        before=[p.read_bytes() for p in files];result=auto_tables.handle(ws,{'action':'list'})
        self.assertEqual(len(result['items']),1);self.assertEqual(len(result['items'][0]['evidence_members']),2)
        self.assertTrue(result['items'][0]['editor']['has_human']);self.assertEqual([p.read_bytes() for p in files],before)
