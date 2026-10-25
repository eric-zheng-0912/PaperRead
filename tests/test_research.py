from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from easyread.research import comparison, index, markdown, proposals, tasks
from easyread.research.store import normalize_folders


class ResearchIndexTest(unittest.TestCase):
    def test_cjk_chunks_stay_below_embedding_safe_character_bound(self):
        body = "研究方法與視覺表徵。" * 120
        text = "---\nresearch_id: test-note\ntype: topic\n---\n\n# 主題\n\n" + body
        note = markdown.parse_note(Path("topic.md"), "topic.md", text)
        pieces = markdown.chunks(note, text)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece) <= 320 for _, piece in pieces))

    def test_search_keeps_cjk_substring_fallback_and_kind_filter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "Research" / "Topics"
            folder.mkdir(parents=True)
            (folder / "vision.md").write_text(
                "---\nresearch_id: topic-1\ntype: topic\n---\n\n# 視覺表徵\n\n跨模態視覺表徵的研究問題。\n", encoding="utf-8")
            state = root / ".research-os"
            state.mkdir()
            with mock.patch.object(index, "vault_path", lambda: root), \
                 mock.patch.object(index, "state_dir", lambda: state), \
                 mock.patch.object(index, "_embedder", lambda: None), \
                 mock.patch.object(tasks, "submit", lambda kind, runner, **kwargs: {"id": "test-task", "kind": kind, "state": "queued"}):
                result = index.search("模態視覺", kind="topic")
                wrong_kind = index.search("模態視覺", kind="question")
            self.assertEqual(len(result["results"]), 1)
            self.assertEqual(result["results"][0]["title"], "視覺表徵")
            self.assertEqual(wrong_kind["results"], [])

    def test_index_folders_reject_paths_outside_vault_and_hidden_folders(self):
        for invalid in ("/outside", "../outside", "Research/../outside", ".private", "Research/.private", "C:\\outside"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_folders([invalid])


class ComparisonTest(unittest.TestCase):
    class Workspace:
        def __init__(self, identity: str, title: str):
            self.id = identity
            self.data = {"paper": {"meta": {"title_en": title, "source_sha256": hashlib.sha256(identity.encode()).hexdigest()},
                                   "blocks": [{"id": "b1", "type": "heading", "en": "Method", "page": 1},
                                              {"id": "b2", "type": "paragraph", "en": "A method using attention.", "page": 2},
                                              {"id": "b3", "type": "paragraph", "en": "Evaluated on Dataset A.", "page": 3}]},
                         "reader": {"notes": {"n1": {"anchor": "b2", "quote": "attention", "body": "Check cost."}},
                                    "paper_note": {"body": "My method summary"}}}

        def load(self, name):
            return self.data.get(name)

    class Library:
        def __init__(self):
            self.items = {"paper-1": ComparisonTest.Workspace("paper-1", "Paper One"),
                          "paper-2": ComparisonTest.Workspace("paper-2", "Paper Two")}

        def ws(self, identity):
            return self.items.get(identity)

    def test_full_paper_context_includes_blocks_and_my_notes(self):
        paper, chunks = comparison._paper_text(self.Library().ws("paper-1"))
        joined = "\n".join(chunks)
        self.assertEqual(paper["title"], "Paper One")
        for expected in ("b1 p.1", "b2 p.2", "b3 p.3", "個人批註", "My method summary"):
            self.assertIn(expected, joined)

    def test_comparison_rejects_pdf_changed_since_parsing(self):
        with tempfile.TemporaryDirectory() as temp:
            ws = self.Library().ws("paper-1")
            ws.root = Path(temp)
            (ws.root / "source.pdf").write_bytes(b"changed PDF")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                comparison._paper_text(ws)

    def test_comparison_stages_a_note_until_user_accepts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / ".research-os"
            def fake_model(_cfg, prompt, *_args):
                return "## 比較範圍\n\n兩篇論文。\n\n## 候選研究問題\n\n待驗證：資料集差異。" if "請輸出：" in prompt else "方法與實驗摘要。"
            def run_now(kind, runner, retry_failed=False, **kwargs):
                self.assertEqual(kind, "comparison")
                return {"state": "completed", "result": runner(threading.Event(), lambda *_args: None)}
            with mock.patch.object(comparison, "vault_path", lambda: root), \
                 mock.patch.object(proposals, "vault_path", lambda: root), \
                 mock.patch("easyread.research.store.vault_path", lambda: root), \
                 mock.patch.object(proposals, "state_dir", lambda: state), \
                 mock.patch.object(comparison.tasks, "submit", run_now), \
                 mock.patch("easyread.chat_models.engine_cfg", lambda *_args: ({"engine": "test"}, {"id": "test", "name": "Test"})), \
                 mock.patch("easyread.engines.for_translation", lambda cfg: cfg), \
                 mock.patch("easyread.engines.run", fake_model):
                result = comparison.start(self.Library(), ["paper-1", "paper-2"], ["核心方法", "限制"])
                proposal = result["result"]["proposal"]
                destination = root / proposal["target"]
                self.assertEqual(proposal["status"], "pending")
                self.assertFalse(destination.exists())
                self.assertEqual(proposals.latest_pending_comparison()["id"], proposal["id"])
                self.assertIn("## 參考論文", proposal["content"])
                self.assertIn("Paper One", proposal["content"])
                accepted = proposals.decide(proposal["id"], accept=True)
                self.assertEqual(accepted["status"], "accepted")
                self.assertTrue(destination.exists())
                index = json.loads((root / "Research" / ".paperread-index.json").read_text(encoding="utf-8"))
                self.assertIn(proposal["target"], {entry.get("path", "") for entry in index["documents"].values()})
                self.assertIsNone(proposals.latest_pending_comparison())

    def test_selected_papers_must_be_unique_and_within_limit(self):
        with mock.patch.object(comparison, "vault_path", lambda: Path("C:/vault")):
            for ids in (["paper-1"], ["paper-1", "paper-1"], ["a", "b", "c", "d", "e", "f"]):
                with self.subTest(ids=ids), self.assertRaises(ValueError):
                    comparison.start(self.Library(), ids, ["核心方法"])


class ProposalConflictTest(unittest.TestCase):
    def test_accept_refuses_to_overwrite_note_changed_after_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            note = root / "Research" / "Questions" / "Existing.md"
            note.parent.mkdir(parents=True)
            original = '---\nresearch_id: "id-1"\ntype: "question"\nplugin_field: "keep"\n---\n\n# Existing\n\nOld body\n'
            note.write_text(original, encoding="utf-8")
            state = root / ".research-os"
            changed = original.replace("Old body", "A concurrent Obsidian edit")
            def concurrent_edit(*args, **kwargs):
                note.write_text(changed, encoding="utf-8")
                return "## Current Evidence\n\nDraft body"
            with mock.patch.object(proposals, "vault_path", lambda: root), \
                 mock.patch("easyread.research.store.vault_path", lambda: root), \
                 mock.patch.object(proposals, "state_dir", lambda: state), \
                 mock.patch.object(proposals.workflows, "gather", lambda *args, **kwargs: {"sources": []}), \
                 mock.patch("easyread.chat_models.engine_cfg", lambda *args, **kwargs: ({"engine": "test"}, {"id": "test", "name": "Test"})), \
                 mock.patch("easyread.engines.for_translation", lambda cfg: cfg), \
                 mock.patch("easyread.engines.run", concurrent_edit):
                proposal = proposals.create("Existing", "question", "Update current evidence", object(), target="Research/Questions/Existing.md")
                result = proposals.decide(proposal["id"], accept=True)
            self.assertEqual(result["status"], "conflict")
            self.assertEqual(note.read_text(encoding="utf-8"), changed)


if __name__ == "__main__":
    unittest.main()
