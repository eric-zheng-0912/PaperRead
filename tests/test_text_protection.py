"""New model candidates and pure helpers only; never migrate saved papers."""
import copy
import unittest

from easyread import checks, consistency, sentences, terms, translate


class TextProtectionTest(unittest.TestCase):
    def test_listing_normalizes_only_new_candidate_and_preserves_source(self):
        block = {'id': 'code1', 'type': 'listing', 'page': 2, 'code': '  x = 2\n',
                 'lang': 'python', 'caption_en': 'Listing 1', 'sents': [[1, 1]]}
        data = translate._normalize({'blocks': [copy.deepcopy(block)]}, [2], set())
        got = data['blocks'][0]
        self.assertEqual(got['type'], 'para')
        self.assertIn('  x = 2\n', got['en'])
        self.assertEqual(got['code'], block['code'])
        self.assertNotIn('zh', got)
        self.assertNotIn('sents', got)

    def test_malformed_listing_is_not_silently_dropped_or_completed(self):
        from easyread import codeblocks
        block = {'id': 'c', 'type': 'code', 'lines': [1]}
        before = copy.deepcopy(block)
        codeblocks.normalize(block)
        self.assertEqual(block, before)
        self.assertTrue(checks.block_problems([block])[0])

    def test_embedded_fences_get_longer_wrapper_and_no_language_injection(self):
        from easyread import codeblocks
        block = {'type': 'code', 'lines': ['```', '<script>'], 'lang': 'bad\n```'}
        codeblocks.normalize(block)
        self.assertTrue(block['en'].startswith('````\n'))
        self.assertIn('```\n<script>', block['en'])

    def test_long_fence_protects_shorter_backticks_and_formula_signals(self):
        from easyread import codeblocks
        text = 'before\n````py\nx = `v`; \\alpha; [^1]\n```\n````\nafter'
        self.assertEqual(codeblocks.prose(text).strip(), 'before\n \nafter')
        self.assertEqual(checks.block_problems([{'id': 'p', 'type': 'para', 'en': text}])[0], [])

    def test_term_replacement_preserves_code_and_all_math_delimiters(self):
        for protected in ('`old`', '```py\nold\n```', '$old$', '$$old$$', r'\(old\)', r'\[old\]'):
            with self.subTest(protected=protected):
                self.assertEqual(terms._swap('old ' + protected, [('old', 'new', [])]), 'new ' + protected)
                self.assertEqual(terms.occurrences('old ' + protected, 'old'), 1)

    def test_english_code_mentions_do_not_trigger_glossary_replacement(self):
        self.assertEqual(terms.mentions_count('`bias` and $bias$', 'bias'), 0)
        self.assertFalse(terms._mentions('`bias`', 'bias'))

    def test_consistency_ignores_code_and_bracket_math(self):
        self.assertEqual(consistency._plain(r'prose `old` \(old\)').strip(), 'prose')

    def test_sentence_markers_inside_code_and_math_remain_literal(self):
        for protected in ('`a. ‖ b.`', r'\(a. ‖ b.\)', r'\[a. ‖ b.\]'):
            with self.subTest(protected=protected):
                clean, _ = sentences.unmark('Start. ‖ ' + protected)
                self.assertIn(protected, clean)

    def test_fenced_blocks_never_receive_sentence_alignment(self):
        text = 'Intro. More.\n```py\nx = 2\n```'
        self.assertEqual(sentences.mark_en(text), (text, []))
        b = {'id': 'p', 'type': 'para', 'en': text, 'zh': text, 'sents': [[1, 1], [2, 2]]}
        sentences.attach([b])
        self.assertNotIn('sents', b)
        self.assertEqual(b['en'], text)

    def test_footnote_guidance_is_separate_from_references(self):
        from easyread import prompts, prompts_en
        for rules in (prompts.RULES, prompts_en.RULES_EN):
            self.assertIn('[^n]', rules)
            self.assertIn('footnote', rules)

    def test_traditional_conversion_preserves_code_and_math_literals(self):
        from easyread import traditional
        text = '软件 `变量 = "软件"` $\\text{软件}$ 软件'
        self.assertEqual(traditional.convert(text), '軟體 `变量 = "软件"` $\\text{软件}$ 軟體')

    def test_real_traditional_merge_preserves_new_listing_bytes(self):
        import tempfile
        from pathlib import Path
        from easyread import paperdata
        from easyread.store import Workspace, write_json_atomic
        with tempfile.TemporaryDirectory() as folder:
            ws = Workspace(Path(folder))
            write_json_atomic(ws.paper_path, {'meta': {'target': 'zh-TW'}, 'blocks': []})
            code = '  软件 = "变量"\n'
            data = translate._normalize({'blocks': [{'id': 'c1', 'type': 'code', 'page': 1,
                'en': code, 'zh': code}]}, [1], set())
            before = copy.deepcopy(data['blocks'][0])
            paperdata.merge_blocks(ws, data, done=[1])
            actual = ws.load('paper')['blocks'][0]
            self.assertEqual(actual['en'], before['en'])
            self.assertEqual(actual['zh'], before['zh'])


if __name__ == '__main__':
    unittest.main()
