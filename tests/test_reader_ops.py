import tempfile
import unittest
from pathlib import Path

from easyread.store import Workspace


class ReaderOpsTest(unittest.TestCase):
    def test_progress_only_changes_navigation_and_revision(self):
        import copy
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace(Path(d))
            ws.apply_reader_ops([
                {'op':'note','note':{'id':'n','body':'manual note'}},
                {'op':'edit','block':'b','zh':'manual edit'},
                {'op':'ai_annotation','annotation':{'annotation_id':'a','anchor':'b','status':'accepted'}},
                {'op':'paper_note','body':'manual paper note'},
            ])
            before = copy.deepcopy(ws.load('reader'))
            attachment = ws.root/'source.pdf'
            attachment.write_bytes(b'synthetic unchanged attachment')
            ws.apply_reader_ops([{'op':'progress','block':'b','ratio':.4,'off':.3,'at':'2099-01-01T00:00:00Z'}])
            after = Workspace(Path(d)).load('reader')
            for key in before.keys() - {'progress','rev'}:
                self.assertEqual(after[key], before[key], key)
            self.assertEqual(after['rev'], before['rev']+1)
            self.assertEqual(after['progress'], {'block':'b','ratio':.4,'off':.3,'at':'2099-01-01T00:00:00Z'})
            self.assertEqual(attachment.read_bytes(), b'synthetic unchanged attachment')

    def test_deletion_wins_over_delayed_creation_in_both_orders(self):
        note = {"op": "note", "note": {"id": "n1", "body": "旧笔记", "updated": "2026-10-01T00:01:00Z"}}
        delete = {"op": "note_del", "id": "n1", "at": "2026-10-01T00:02:00Z"}
        for ops in [[note, delete], [delete, note]]:
            with self.subTest(ops=ops), tempfile.TemporaryDirectory() as d:
                ws = Workspace(Path(d))
                for op in ops:
                    ws.apply_reader_ops([op])
                self.assertTrue(ws.load("reader")["notes"]["n1"]["deleted"])
                # Explicitly restoring with a newer edit is still possible.
                ws.apply_reader_ops([{**note, "note": {**note["note"], "updated": "2026-10-01T00:03:00Z"}}])
                self.assertFalse(ws.load("reader")["notes"]["n1"].get("deleted", False))

    def test_reading_position_keeps_block_offset_and_ratio(self):
        """回到上次读到的地方要用这三样：哪一块、线在块里的位置、全文比例。"""
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace(Path(d))
            ws.apply_reader_ops([{"op": "progress", "block": "p2", "off": 0.32, "ratio": 0.43, "at": "2026-10-01T00:01:00Z"}])
            self.assertEqual(ws.load("reader")["progress"],
                             {"block": "p2", "off": 0.32, "ratio": 0.43, "at": "2026-10-01T00:01:00Z"})
            # 更早的位置晚到，不覆盖
            ws.apply_reader_ops([{"op": "progress", "block": "p1", "off": 0.9, "ratio": 0.1, "at": "2026-10-01T00:00:00Z"}])
            self.assertEqual(ws.load("reader")["progress"]["block"], "p2")
            # 1.4.2 及以前的操作只带 ratio，照旧能落盘
            ws.apply_reader_ops([{"op": "progress", "block": "p5", "ratio": 0.6, "at": "2026-10-01T00:02:00Z"}])
            self.assertEqual(ws.load("reader")["progress"]["ratio"], 0.6)
            self.assertNotIn("off", ws.load("reader")["progress"])
