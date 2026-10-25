"""Real spawned processes; all files belong to isolated temporary workspaces."""
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import time
import unittest

from easyread.store import Workspace, dir_lock, write_json_atomic


def hold(root, ready, release, age=False):
    with dir_lock(Path(root), timeout=5):
        if age:
            stamp = time.time() - 120
            os.utime(Path(root) / '.write.lock', (stamp, stamp))
        ready.set()
        if not release.wait(45):
            raise RuntimeError('Test holder was not released')


def write_updates(root, start, worker, count):
    ws = Workspace(Path(root))
    if not start.wait(10):
        raise RuntimeError('Test writers did not start')
    for i in range(count):
        def update(paper):
            n = paper['count']
            time.sleep(0.002)  # Expose overlapping read/modify/write sections.
            paper['count'] = n + 1
        ws.update('paper', update)
        ws.apply_reader_ops([{'op': 'note', 'note': {
            'id': f'{worker}-{i}', 'body': '繁體／简体筆記',
            'updated': '2026-10-20T00:00:00Z'}}])


class StoreLockTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctx = multiprocessing.get_context('spawn')
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for process in self.children:
            if process.is_alive():
                process.terminate()
            process.join(5)

    def holder(self, age=False):
        ready, release = self.ctx.Event(), self.ctx.Event()
        process = self.ctx.Process(target=hold, args=(str(self.root), ready, release, age))
        process.start()
        self.children.append(process)
        self.assertTrue(ready.wait(10), 'Holder failed to acquire lock')
        return process, release

    def finish(self, process, release):
        release.set()
        process.join(10)
        self.assertEqual(process.exitcode, 0)

    def assert_busy(self):
        with self.assertRaises(TimeoutError):
            with dir_lock(self.root, timeout=0.15):
                self.fail('Live holder was bypassed')

    def test_aged_live_holder_cannot_be_stolen(self):
        process, release = self.holder(age=True)
        self.assert_busy()
        self.finish(process, release)

    def test_old_holder_release_keeps_successor_lock_identity(self):
        process, release = self.holder(age=True)
        before = (self.root / '.write.lock').stat().st_ino
        ready, successor_release = self.ctx.Event(), self.ctx.Event()
        successor = self.ctx.Process(target=hold, args=(str(self.root), ready, successor_release))
        successor.start()
        self.children.append(successor)
        self.assertFalse(ready.wait(0.3), 'Successor bypassed the live owner')
        self.finish(process, release)
        self.assertTrue((self.root / '.write.lock').exists(), 'Release unlinked the rendezvous file')
        self.assertTrue(ready.wait(10), 'Waiting successor did not acquire the released lock')
        self.assertEqual((self.root / '.write.lock').stat().st_ino, before)
        self.assert_busy()
        self.finish(successor, successor_release)

    def test_live_holder_over_thirty_seconds(self):
        process, release = self.holder()
        self.assertFalse(release.wait(30.2))
        self.assert_busy()
        self.finish(process, release)

    def test_terminated_process_releases_lock_without_stale_timeout(self):
        process, _ = self.holder()
        process.terminate()
        process.join(10)
        self.assertFalse(process.is_alive())
        with dir_lock(self.root, timeout=1):
            pass

    def test_timeout_does_not_release_owner_and_waiter_later_succeeds(self):
        process, release = self.holder()
        self.assert_busy()
        self.assert_busy()
        self.finish(process, release)
        with dir_lock(self.root, timeout=1):
            pass

    def test_exception_releases_lock_and_preserves_legacy_lock_file(self):
        lock = self.root / '.write.lock'
        lock.write_bytes(b'old-pid-marker')
        before = lock.read_bytes()
        with self.assertRaisesRegex(ValueError, 'synthetic'):
            with dir_lock(self.root, timeout=0):
                raise ValueError('synthetic')
        with dir_lock(self.root, timeout=0):
            self.assertEqual(lock.stat().st_size, len(before))
        self.assertEqual(lock.read_bytes(), before)

    def test_threads_in_one_process_are_also_exclusive(self):
        with dir_lock(self.root):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(self.assert_busy).result(timeout=5)

    def test_empty_legacy_and_custom_name(self):
        lock = self.root / '.proposal-review.lock'
        lock.touch()
        with dir_lock(self.root, lock.name, timeout=0):
            self.assertTrue(lock.exists())
        self.assertTrue(lock.exists())

    def test_concurrent_workspace_updates_preserve_notes_and_translations(self):
        original = {'count': 0, 'blocks': [
            {'id': 'tw', 'en': 'source', 'zh': '繁體譯文'},
            {'id': 'cn', 'en': 'source', 'zh': '简体译文'}],
            'table_cache': {'saved': 'unchanged'}}
        write_json_atomic(self.root / 'paper.json', original)
        history = self.root / 'history'
        history.mkdir()
        (history / 'legacy.json').write_bytes(b'original history')
        start = self.ctx.Event()
        processes = [self.ctx.Process(target=write_updates, args=(str(self.root), start, n, 12))
                     for n in range(4)]
        for process in processes:
            process.start()
            self.children.append(process)
        start.set()
        for process in processes:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        ws = Workspace(self.root)
        self.assertEqual(ws.load('paper'), {**original, 'count': 48})
        self.assertEqual(len(ws.load('reader')['notes']), 48)
        self.assertEqual((history / 'legacy.json').read_bytes(), b'original history')


if __name__ == '__main__':
    unittest.main()
