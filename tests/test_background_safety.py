"""Synthetic libraries and mock workers; no real API or user storage."""
import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from easyread import jobs, store
from easyread.library import Library
from easyread.research import tasks


class StopWorker(Exception):
    pass


class BackgroundSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lib = Library(Path(self.tmp.name))
        root = self.lib.root / 'paper001'; root.mkdir()
        store.write_json_atomic(root / 'paper.json', {'meta': {}, 'blocks': []})
        self.ws = store.Workspace(root)
        self.ws.source_pdf.write_bytes(b'%PDF synthetic original')
        with patch('easyread.jobs.threading.Thread.start'):
            self.jobs = jobs.Jobs(self.lib)

    def drain(self, small=False):
        queue = self.jobs.small if small else self.jobs.bulk
        get = queue.get
        def pop():
            if queue.empty(): raise StopWorker()
            return get()
        with patch.object(queue, 'get', side_effect=pop), self.assertRaises(StopWorker):
            (self.jobs._small_loop if small else self.jobs._bulk_loop)()

    def test_cancelled_full_worker_cannot_write_late_result(self):
        before = self.ws.paper_path.read_bytes()
        self.jobs.enqueue(self.ws)
        def work(ws, job, cancel):
            self.jobs.cancel(ws.id)
            ws.update('paper', lambda p:p.update(late='must not save'))
        with patch.object(self.jobs, '_run_bulk', side_effect=work): self.drain()
        self.assertEqual(self.ws.paper_path.read_bytes(), before)
        self.assertEqual(self.ws.load('job')['state'], 'cancelled')

    def test_external_edit_during_response_is_not_overwritten(self):
        self.jobs.enqueue(self.ws)
        def work(ws, *_):
            # A separate UI/request context does not inherit the worker fence.
            from easyread.background import suspend
            with suspend(): ws.update('paper', lambda p:p.update(manual='keep'))
            ws.update('paper', lambda p:p.update(late='reject'))
        with patch.object(self.jobs, '_run_bulk', side_effect=work): self.drain()
        self.assertEqual(self.ws.load('paper')['manual'], 'keep')
        self.assertNotIn('late', self.ws.load('paper'))

    def test_source_changed_after_queue_is_rejected(self):
        self.jobs.enqueue(self.ws)
        self.ws.source_pdf.write_bytes(b'%PDF changed source')
        with patch.object(self.jobs, '_run_bulk') as run: self.drain()
        run.assert_not_called()

    def test_old_owner_cannot_save_after_restart(self):
        from easyread.background import Fence, bind, snapshot
        old = Fence(self.ws, threading.Event(), snapshot(self.ws), owner=lambda: self.ws.load('job').get('run_id')=='old')
        store.write_json_atomic(self.ws.root/'job.json', {'run_id':'new'})
        with self.assertRaises(Exception), bind(old): self.ws.update('paper', lambda p:p.update(old=True))
        self.assertNotIn('old', self.ws.load('paper'))

    def test_small_cancel_is_scoped_and_reaches_retranslate(self):
        first = self.jobs.submit_small('retranslate', self.ws.id, key='p1')
        second = self.jobs.submit_small('answer', self.ws.id, note='n1')
        self.jobs.cancel_small(first['id'])
        with patch('easyread.jobs.config.load', return_value={}), patch('easyread.jobs.translate.retranslate') as translate, \
                patch('easyread.jobs.translate.answer') as answer:
            self.drain(True)
        translate.assert_not_called(); answer.assert_called_once()
        self.assertIsNotNone(answer.call_args.args[-1])
        self.assertEqual(self.jobs.small_status()[0]['id'], second['id'])

    def test_small_restart_is_persistent_and_never_replays_api(self):
        first = self.jobs.submit_small('simple-table', self.ws.id)
        with patch('easyread.jobs.threading.Thread.start'):
            reopened = jobs.Jobs(self.lib)
        status = next(j for j in reopened.small_status() if j['id']==first['id'])
        self.assertEqual(status['state'], 'cancelled')
        self.assertTrue(reopened.small.empty())

    def test_foreign_workspace_cannot_be_enqueued(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError): self.jobs.enqueue(store.Workspace(Path(root)))

    def test_fence_preserves_notes_and_manual_table_snapshot(self):
        from easyread.background import Fence, bind, snapshot, suspend
        (self.ws.root/'manual_table_links.json').write_text('{"revision":1}')
        fence=Fence(self.ws, threading.Event(), snapshot(self.ws))
        with bind(fence):
            self.ws.update('paper',lambda p:p.update(first=True))
            with suspend(): self.ws.apply_reader_ops([{'op':'note','note':{'id':'n','body':'manual'}}])
            with self.assertRaises(Exception): self.ws.update('paper',lambda p:p.update(late=True))
        self.assertNotIn('late',self.ws.load('paper'))
        self.assertEqual(self.ws.load('reader')['notes']['n']['body'],'manual')
        self.assertEqual((self.ws.root/'manual_table_links.json').read_text(),'{"revision":1}')

    def test_real_translation_pool_discards_mock_response_after_cancel(self):
        from concurrent.futures import ThreadPoolExecutor
        from easyread import translate
        ready, release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        self.ws.update('paper',lambda p:p.update(meta={'target':'zh-TW','page_count':1,'pages':[{'n':1}]},
            translation={'done_pages':[]},references=[],glossary=[]))
        before=self.ws.paper_path.read_bytes()
        self.jobs.enqueue(self.ws,pages=[1])
        cfg={'engine':'openai','openai':{'vision':False},'batch_pages':1}
        def response(*_):
            ready.set(); release.wait(3)
            return json.dumps({'blocks':[{'id':'p1','type':'para','page':1,'en':'Original.','zh':'Late result.'}]})
        def work(ws,job,cancel):
            translate.translate_pages(ws,cfg,[1],cancel,lambda *_:None)
        with patch.object(translate.engines,'run',side_effect=response), patch.object(translate.pdfwork,'locate'), \
                patch.object(self.jobs,'_run_bulk',side_effect=work), ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.drain)
            self.assertTrue(ready.wait(2))
            self.jobs.cancel(self.ws.id)
            self.assertFalse(future.done(), 'Mock remote request still runs; cancellation is not transport abort')
            release.set(); future.result(4)
        self.assertEqual(self.ws.paper_path.read_bytes(),before)

    def test_expired_worker_is_rejected(self):
        from easyread.background import Fence,bind,snapshot
        fence=Fence(self.ws,threading.Event(),snapshot(self.ws),expires=0)
        with self.assertRaises(ValueError), bind(fence):
            self.ws.update('paper',lambda p:p.update(expired=True))
        self.assertNotIn('expired',self.ws.load('paper'))

    def test_guard_checks_source_inside_transaction_and_allows_reading_progress(self):
        from easyread.background import Fence,bind,snapshot,suspend
        fence=Fence(self.ws,threading.Event(),snapshot(self.ws))
        with bind(fence):
            with suspend(): self.ws.apply_reader_ops([{'op':'progress','block':'p1'}])
            self.ws.update('paper',lambda p:p.update(first=True))
            self.ws.source_pdf.write_bytes(b'%PDF replaced')
            with self.assertRaises(ValueError): self.ws.update('paper',lambda p:p.update(late=True))
        self.assertNotIn('late',self.ws.load('paper'))

    def test_prior_translation_snapshot_is_immutable_and_once_per_job(self):
        from easyread.background import Fence,bind,snapshot
        self.ws.update('paper',lambda p:p.update(blocks=[{'id':'p','type':'para','zh':'old translation'}]))
        before=self.ws.paper_path.read_bytes()
        with bind(Fence(self.ws,threading.Event(),snapshot(self.ws))):
            self.ws.update('paper',lambda p:p['blocks'][0].update(zh='new requested translation'))
            self.ws.update('paper',lambda p:p.update(done=True))
        archives=list((self.ws.root/'history').glob('background-*-paper.json'))
        self.assertEqual(len(archives),1)
        self.assertEqual(archives[0].read_bytes(),before)

    def test_restart_revokes_running_small_worker_without_status_overwrite(self):
        from concurrent.futures import ThreadPoolExecutor
        ready,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        self.jobs.submit_small('answer',self.ws.id,note='n')
        def late(ws,*args):
            ready.set();release.wait(3)
            ws.update('paper',lambda p:p.update(late=True))
        with patch('easyread.jobs.config.load',return_value={}),patch('easyread.jobs.translate.answer',side_effect=late),ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.drain,True);self.assertTrue(ready.wait(2))
            with patch('easyread.jobs.threading.Thread.start'):
                reopened=jobs.Jobs(self.lib)
            release.set();future.result(4)
        self.assertNotIn('late',self.ws.load('paper'))
        self.assertEqual(store.read_json(self.jobs._small_path())['jobs'][reopened.recent[0]['id']]['state'],'cancelled')

    def test_claim_is_atomic_and_rotates_generation(self):
        self.jobs.enqueue(self.ws)
        old=self.ws.load('job')
        claimed=self.jobs._claim(self.ws,old)
        self.assertNotEqual(claimed['run_id'],old['run_id'])
        with self.assertRaises(ValueError):self.jobs._claim(self.ws,old)

    def test_persisted_cancel_request_is_checked_even_without_local_event(self):
        self.jobs.enqueue(self.ws)
        claimed=self.jobs._claim(self.ws,self.ws.load('job'))
        self.jobs.cancel(self.ws.id)
        self.assertTrue(self.ws.load('job')['cancel_requested'])
        from easyread.engines import Cancelled
        with self.assertRaises(Cancelled):self.jobs._owns_bulk(self.ws,claimed)

    def test_corrupt_small_registry_preserved_without_blocking_startup(self):
        self.jobs._small_path().write_bytes(b'{broken')
        with patch('easyread.jobs.threading.Thread.start'):
            reopened=jobs.Jobs(self.lib)
        self.assertEqual(self.jobs._small_path().read_bytes(),b'{broken')
        with self.assertRaises(ValueError):reopened.submit_small('answer',self.ws.id)

    def test_terminal_status_is_visible_only_after_persistence(self):
        from concurrent.futures import ThreadPoolExecutor
        self.jobs.submit_small('answer',self.ws.id,note='n')
        ready,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        persist=self.jobs._persist_small
        def delayed():
            if self.jobs.recent[0]['state']=='done' and not release.is_set():
                ready.set();release.wait(3)
            persist()
        with patch('easyread.jobs.config.load',return_value={}),patch('easyread.jobs.translate.answer'), \
                patch.object(self.jobs,'_persist_small',side_effect=delayed),ThreadPoolExecutor(2) as pool:
            worker=pool.submit(self.drain,True);self.assertTrue(ready.wait(2))
            polling=pool.submit(self.jobs.small_status)
            self.assertFalse(polling.done())
            release.set();worker.result(4);state=polling.result(4)[0]
        self.assertEqual(state['state'],'done')
        self.assertEqual(store.read_json(self.jobs._small_path())['jobs'][state['id']]['state'],'done')

    def test_full_restart_revokes_inflight_owner_before_any_late_write(self):
        from easyread.background import Fence,bind,snapshot
        self.jobs.enqueue(self.ws)
        claimed=self.jobs._claim(self.ws,self.ws.load('job'))
        fence=Fence(self.ws,threading.Event(),snapshot(self.ws),owner=lambda:self.jobs._owns_bulk(self.ws,claimed))
        with patch('easyread.jobs.threading.Thread.start'):
            reopened=jobs.Jobs(self.lib)
        self.assertTrue(reopened.bulk.empty())
        self.assertEqual(self.ws.load('job')['state'],'cancelled')
        with self.assertRaises(Exception),bind(fence):self.ws.update('paper',lambda p:p.update(late=True))
        self.assertNotIn('late',self.ws.load('paper'))

    def test_verified_queued_work_can_resume_but_unknown_legacy_work_cannot(self):
        self.jobs.enqueue(self.ws)
        old=self.ws.load('job')['run_id']
        with patch('easyread.jobs.threading.Thread.start'):
            reopened=jobs.Jobs(self.lib)
        self.assertEqual(reopened.bulk.qsize(),1)
        self.assertNotEqual(self.ws.load('job')['run_id'],old)
        store.write_json_atomic(self.ws.root/'job.json',{'state':'queued'})
        with patch('easyread.jobs.threading.Thread.start'):
            legacy=jobs.Jobs(self.lib)
        self.assertTrue(legacy.bulk.empty())
        self.assertEqual(self.ws.load('job')['state'],'cancelled')


class ResearchScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_library_and_kind_isolation(self):
        gate=threading.Event(); self.addCleanup(gate.set)
        def run(cancel,report): gate.wait(3); return 'done'
        a=tasks.submit('comparison',run,scope='A')
        b=tasks.submit('comparison',run,scope='B')
        c=tasks.submit('index',run,scope='A')
        self.assertEqual(len({a['id'],b['id'],c['id']}),3)
        self.assertIsNone(tasks.get(a['id'],scope='B'))
        self.assertIsNone(tasks.cancel(a['id'],scope='B'))
        self.assertNotIn(a['id'],[t['id'] for t in tasks.list_tasks(scope='B')])
        tasks.cancel(a['id'],scope='A')
        self.assertNotEqual(tasks.get(b['id'],scope='B')['state'],'cancelling')

    def test_cancel_linearizes_with_result_commit(self):
        ready=threading.Event(); gate=threading.Event(); saved=[]
        self.addCleanup(gate.set)
        def run(cancel,report):
            ready.set(); gate.wait(3)
            return tasks.commit(cancel,lambda:saved.append('bad'))
        task=tasks.submit('late-test',run,scope='late')
        self.assertTrue(ready.wait(2)); tasks.cancel(task['id'],scope='late'); gate.set()
        import time
        for _ in range(200):
            if tasks.get(task['id'],scope='late')['state']=='cancelled': break
            time.sleep(.01)
        self.assertEqual(saved,[])

    def test_persisted_running_state_is_interrupted_on_recovery(self):
        path=self.root/'tasks.json'
        store.write_json_atomic(path,{'version':1,'tasks':{'old':{'id':'old','kind':'comparison','scope':'R',
            'state':'running','created':1,'updated':1,'_session':'previous'}}})
        state=tasks.list_tasks(scope='R',storage=path)
        self.assertEqual(state[0]['state'],'cancelled')
        self.assertNotIn('_session',state[0])
        self.assertEqual(store.read_json(path)['tasks']['old']['state'],'cancelled')

    def test_real_process_recovery_revokes_old_worker_commit(self):
        import subprocess,sys
        path=self.root/'restart.json'
        ready,release=threading.Event(),threading.Event(); saved=[]
        self.addCleanup(release.set)
        def work(cancel,report):
            ready.set();release.wait(4)
            return tasks.commit(cancel,lambda:saved.append('late'))
        task=tasks.submit('restart',work,scope='restart',storage=path)
        self.assertTrue(ready.wait(2))
        code="from easyread.research import tasks; from pathlib import Path; import sys; tasks.list_tasks(scope='restart',storage=Path(sys.argv[1]))"
        result=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        release.set()
        import time
        for _ in range(100):
            if task['id'] not in tasks._CANCEL:break
            time.sleep(.01)
        self.assertEqual(saved,[])
        self.assertEqual(store.read_json(path)['tasks'][task['id']]['state'],'cancelled')

    def test_derived_index_databases_are_separate_per_library(self):
        from easyread.research import index
        from types import SimpleNamespace
        with patch.object(index,'state_dir',return_value=self.root):
            a=index._db(SimpleNamespace(root=self.root/'A'))
            b=index._db(SimpleNamespace(root=self.root/'B'))
            try:
                self.assertNotEqual(a.execute('PRAGMA database_list').fetchone()[2],b.execute('PRAGMA database_list').fetchone()[2])
            finally:a.close();b.close()

    def test_index_rejects_cancelled_and_changed_source_after_embedding(self):
        from easyread.research import index
        from easyread.background import CancelEvent
        path=self.root/'note.md'
        text='# Test\n\nSource content for the derived index.'
        class Encoder:
            def __init__(self,hook):self.hook=hook
            def embed(self,passages,**kwargs):
                self.hook()
                for _ in passages:yield [1.0]
        with patch.object(index,'state_dir',return_value=self.root):
            db=index._db()
            try:
                for mode in ('cancel','changed'):
                    path.write_text(text,encoding='utf-8')
                    event=CancelEvent()
                    hook=event.set if mode=='cancel' else lambda:path.write_text('Changed by user',encoding='utf-8')
                    with self.subTest(mode=mode),self.assertRaises(Exception):
                        index._index_one(db,self.root,path,'note.md',text,Encoder(hook),path.stat(),event)
                    self.assertEqual(db.execute('SELECT count(*) FROM chunks').fetchone()[0],0)
            finally:db.close()


if __name__=='__main__': unittest.main()
