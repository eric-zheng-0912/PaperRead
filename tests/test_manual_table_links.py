"""Confirmed links with synthetic PDFs, isolated storage and mock translation."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest import mock

from easyread import build, manual_table_links as links, reader_data, simple_tables
from easyread.store import Workspace, write_json_atomic
from tests import test_simple_tables as simple_tests
from tests.test_table_reading_translation import response


class ManualTableLinksTest(unittest.TestCase):
    setUp = simple_tests.SimpleTablesTest.setUp
    imported = simple_tests.SimpleTablesTest.imported
    legacy = simple_tests.SimpleTablesTest.legacy
    protected = simple_tests.SimpleTablesTest.protected
    select = simple_tests.SimpleTablesTest.select
    translate = simple_tests.SimpleTablesTest.translate

    def prepare(self, name=None):
        ws = self.imported(name) if name else self.legacy()
        if name:
            ws.update('paper', lambda p: p.update(blocks=[copy.deepcopy({
                'id':'tab1','type':'table','page':1,'caption_en':'Selected source'})]))
        regions = self.select(ws, (1,2) if name == 'continued_unlabelled' else (1,))
        self.translate(ws, regions)
        record = simple_tables.saved(ws)[0]
        return ws, record

    def request(self, ws, record, bid='tab1'):
        args = {'record_id':record['id'], 'translation_hash':record['table_reading_cache']['translation_hash']}
        options = links.handle(ws, {'action':'link-targets', **args})
        target = next(t for t in options['targets'] if t['id']==bid)
        return {'action':'link','confirmed':True,'revision':options['revision'],
                'block_id':bid,'block_hash':target['block_hash'], **args}

    def test_explicit_confirmation_same_page_target_and_no_legacy_writes(self):
        ws, record = self.prepare()
        ws.update('paper', lambda p: p['blocks'].append({**self.old_table,'id':'tab2'}))
        protected = {p:p.read_bytes() for p in (ws.paper_path,ws.reader_path,ws.source_pdf,self.settings)}
        body = self.request(ws, record, 'tab2')
        self.assertFalse((ws.root/links.FILE).exists())
        with self.assertRaises(ValueError): links.handle(ws, {**body,'confirmed':False})
        links.handle(ws, body)
        paper, _ = reader_data.reader_paper(ws)
        self.assertEqual([b['id'] for b in paper['blocks'] if links.VIEW in b], ['tab2'])
        self.assertEqual({p:p.read_bytes() for p in protected}, protected)

    def test_reopen_and_unlink_do_not_call_model_or_delete_translation(self):
        ws, record = self.prepare()
        links.handle(ws, self.request(ws, record))
        reopened = Workspace(ws.root)
        with mock.patch.object(simple_tables, 'translate', side_effect=AssertionError('No inference')):
            projected, before = reader_data.reader_paper(reopened)
        block = next(b for b in projected['blocks'] if b['id']=='tab1')
        self.assertTrue(block[links.VIEW]['previews'][0].startswith('data:image/webp;base64,'))
        links.handle(ws, {'action':'unlink','confirmed':True,'revision':1,'block_id':'tab1'})
        projected, after = reader_data.reader_paper(ws)
        self.assertNotEqual(before, after)
        self.assertTrue(all(links.VIEW not in b for b in projected['blocks']))
        self.assertEqual(simple_tables.saved(ws), [record])

    def test_retranslation_needs_new_confirmation_and_failed_cancel_preserves_link(self):
        ws, old = self.prepare()
        links.handle(ws, self.request(ws, old))
        before = (ws.root/links.FILE).read_bytes()
        def newer(*args): return response(*args).replace('\\u7ffb\\u8b6f', '\\u65b0\\u8b6f')
        runner=mock.Mock(side_effect=newer)
        result=self.translate(ws, self.select(ws), run=runner, refresh=True)
        self.assertFalse(result['cached']); self.assertEqual(runner.call_count,1)
        new = simple_tables.saved(ws)[0]
        self.assertNotEqual(old['table_reading_zh'],new['table_reading_zh'])
        projected, _ = reader_data.reader_paper(ws)
        self.assertEqual(next(b for b in projected['blocks'] if b['id']=='tab1')[links.VIEW]['values'],old['table_reading_zh'])
        with self.assertRaises(ValueError): links.handle(ws,self.request_stale(old))
        links.handle(ws, self.request(ws,new))
        self.cfg['model'] = 'failure'
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(Exception): self.translate(ws,self.select(ws),cancel=cancel)
        self.assertNotEqual((ws.root/links.FILE).read_bytes(),before)
        current=(ws.root/links.FILE).read_bytes()
        with self.assertRaises(ValueError):
            self.translate(ws,self.select(ws),run=lambda *a:'{}')
        self.assertEqual((ws.root/links.FILE).read_bytes(),current)

    def request_stale(self, record):
        return {'action':'link-targets','record_id':record['id'],
                'translation_hash':record['table_reading_cache']['translation_hash']}

    def test_parallel_revision_and_block_change_are_rejected(self):
        ws, record=self.prepare()
        first=self.request(ws,record)
        second=copy.deepcopy(first)
        gate=threading.Barrier(2)
        def submit(body):
            gate.wait(timeout=5)
            try:
                links.handle(ws,body)
                return 'saved'
            except ValueError:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(submit,[first,second]))
        self.assertEqual(sorted(results),['conflict','saved'])
        before=(ws.root/links.FILE).read_bytes()
        with self.assertRaises(ValueError): links.handle(ws,second)
        changed=self.request(ws,record)
        ws.update('paper',lambda p:p['blocks'][1].update(caption_en='Different source'))
        with self.assertRaises(ValueError): links.handle(ws,changed)
        self.assertEqual((ws.root/links.FILE).read_bytes(),before)
        projected,_=reader_data.reader_paper(ws)
        self.assertTrue(all(links.VIEW not in b for b in projected['blocks']))

    def test_changed_pdf_hides_snapshot_without_modifying_files(self):
        ws, record=self.prepare()
        links.handle(ws,self.request(ws,record))
        before=(ws.root/links.FILE).read_bytes()
        ws.source_pdf.write_bytes(ws.source_pdf.read_bytes()+b'\n% changed')
        projected,_=reader_data.reader_paper(ws)
        self.assertTrue(all(links.VIEW not in b for b in projected['blocks']))
        self.assertEqual((ws.root/links.FILE).read_bytes(),before)
        block=next(b for b in projected['blocks'] if b['id']=='tab1')
        self.assertEqual(block['manual_table_link_revision'],1)
        links.handle(ws,{'action':'unlink','confirmed':True,'revision':1,'block_id':'tab1'})
        self.assertEqual(links.state(ws)['links'],{})

    def test_tampered_snapshot_and_caller_projection_are_not_trusted(self):
        ws, record=self.prepare()
        links.handle(ws,self.request(ws,record))
        state=links.state(ws)
        state['links']['tab1']['record']['table_reading_zh'][0]['zh']='Invented 999'
        write_json_atomic(ws.root/links.FILE,state)
        raw=ws.load('paper'); raw['blocks'][0][links.VIEW]={'unsafe':'HTML'}
        projected,_=reader_data.reader_paper(ws,raw)
        self.assertTrue(all(links.VIEW not in b for b in projected['blocks']))

    def test_multi_page_link_is_ordered_regions_not_rebuilt_cells(self):
        ws, record=self.prepare('continued_unlabelled')
        links.handle(ws,self.request(ws,record))
        projected,_=reader_data.reader_paper(ws)
        data=projected['blocks'][0][links.VIEW]
        self.assertEqual([r['page'] for r in data['source']['regions']],[1,2])
        self.assertEqual(data['source']['coverage'],'selected_regions_only')
        self.assertEqual(len(data['previews']),2)
        self.assertNotIn('rows_zh',data)

    def test_embedded_reader_uses_same_verified_link(self):
        ws, record=self.prepare()
        links.handle(ws,self.request(ws,record))
        ws.update('paper',lambda p:[page.setdefault('img','pages/unused.webp') for page in p['meta']['pages']])
        path=build.build(ws, out=ws.root/'isolated.html')
        html=path.read_text(encoding='utf-8')
        payload=html.split('<script id="pr-data" type="application/json">',1)[1].split('</script>',1)[0]
        data=json.loads(payload)
        self.assertTrue(any(links.VIEW in b for b in data['paper']['blocks']))

    def test_duplicate_block_ids_and_non_table_targets_are_rejected(self):
        ws, record=self.prepare()
        request=self.request(ws,record)
        ws.update('paper',lambda p:p['blocks'].append(copy.deepcopy(p['blocks'][1])))
        with self.assertRaises(ValueError): links.handle(ws,request)
        options=links.handle(ws,self.request_stale(record))
        self.assertEqual(options['targets'],[])

    def test_malformed_sidecar_is_not_overwritten(self):
        ws, record=self.prepare(); request=self.request(ws,record)
        (ws.root/links.FILE).write_bytes(b'{broken')
        with self.assertRaises(ValueError): links.handle(ws,request)
        projected,_=reader_data.reader_paper(ws)
        self.assertTrue(all(links.VIEW not in b for b in projected['blocks']))
        self.assertEqual((ws.root/links.FILE).read_bytes(),b'{broken')
