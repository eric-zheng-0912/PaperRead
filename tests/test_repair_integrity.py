"""Delimiter-only repair with mocked model calls and synthetic source records."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from easyread import checks, translate
from easyread.store import Workspace, write_json_atomic


class FormulaChecksTest(unittest.TestCase):
    def problems(self, text):
        return checks.block_problems([{'id': 'p1', 'type': 'para', 'en': text}])

    def test_bare_tex_is_reported(self):
        for text in (r'Value \alpha = 2.', r'Ratio \frac{1}{2}.', r'x_{i} is measured.'):
            with self.subTest(text=text):
                self.assertTrue(self.problems(text)[0])

    def test_four_delimiters_are_recognized(self):
        for text in (r'$\alpha$', r'$$\alpha$$', r'\(\alpha\)', r'\[\alpha\]'):
            with self.subTest(text=text):
                problems, tex = self.problems(text)
                self.assertEqual(problems, [])
                self.assertEqual(len(tex), 1)
                self.assertEqual(tex[0][1], r'\alpha')

    def test_code_paths_prices_and_plain_variables_not_bare_tex(self):
        for text in (r'`\alpha`', '```tex\n\\frac{1}{2}\n```', r'C:\alpha\file', 'x/y costs 20 USD.'):
            with self.subTest(text=text):
                self.assertEqual(self.problems(text)[0], [])

    def test_unclosed_bracket_delimiter_is_reported(self):
        self.assertTrue(self.problems(r'Value \(x = 2.')[0])

    def test_unclosed_display_delimiter_is_reported(self):
        self.assertTrue(self.problems('Value $$x = 2.')[0])

    def test_real_local_katex_accepts_valid_and_rejects_broken_tex(self):
        self.assertIsNotNone(checks.shutil.which('node'))
        self.assertEqual(checks.tex_problems([('p1', r'\frac{1}{2}', False)]), [])
        self.assertTrue(checks.tex_problems([('p1', r'\frac{', False)]))

    def test_bilingual_cells_and_footnotes_are_checked(self):
        block = {'id': 't1', 'type': 'table', 'head': [['A']], 'rows': [['1']],
                 'head_en': [[r'\alpha']], 'rows_zh': [[r'\beta']],
                 'footnotes_en': [r'\gamma']}
        self.assertGreaterEqual(len(checks.block_problems([block])[0]), 3)


class RepairGuardTest(unittest.TestCase):
    def setUp(self):
        self.data = {'blocks': [{'id': 't1', 'type': 'table', 'page': 1,
            'en': r'Value \alpha = 2 mg; p < 0.05 [1].',
            'head': [['Group', 'Mean']], 'rows': [['A', '2 ± 0.1 mg']],
            'footnotes_en': ['* p < 0.05 [1]'],
            'source': {'pdf_sha256': 'abc', 'page': 1}, 'flag': False}],
            'references': [{'id': '1', 'text': 'Reference 2020.'}]}

    def guard(self, other):
        from easyread.repair_guard import retains_content
        return retains_content(self.data, other)

    def test_delimiter_only_repair_accepted_without_mutating_input(self):
        original = copy.deepcopy(self.data)
        other = copy.deepcopy(self.data)
        other['blocks'][0]['en'] = r'Value $\alpha = 2$ mg; p < 0.05 [1].'
        self.assertTrue(self.guard(other))
        self.assertEqual(self.data, original)

    def test_changed_identity_source_fields_and_false_values_rejected(self):
        for key, value in (('id', 't2'), ('type', 'para'), ('page', 2),
                           ('source', {'pdf_sha256': 'def', 'page': 1}), ('flag', True)):
            with self.subTest(key=key):
                other = copy.deepcopy(self.data)
                other['blocks'][0][key] = value
                self.assertFalse(self.guard(other))

    def test_missing_and_added_blocks_fields_cells_rejected(self):
        for change in ('block', 'field', 'cell', 'extra'):
            with self.subTest(change=change):
                other = copy.deepcopy(self.data)
                if change == 'block': other['blocks'] = []
                if change == 'field': del other['blocks'][0]['flag']
                if change == 'cell': other['blocks'][0]['rows'][0].pop()
                if change == 'extra': other['blocks'][0]['rows'][0].append('invented')
                self.assertFalse(self.guard(other))

    def test_text_numbers_units_statistics_citations_and_footnotes_rejected(self):
        for old, new in (('Value ', ''), ('2 mg', '3 mg'), ('2 mg', '2 g'),
                         ('p <', 'p >'), ('[1]', '[2]'), ('0.05', '0.5')):
            with self.subTest(old=old):
                other = copy.deepcopy(self.data)
                other['blocks'][0]['en'] = other['blocks'][0]['en'].replace(old, new)
                self.assertFalse(self.guard(other))
        other = copy.deepcopy(self.data)
        other['blocks'][0]['footnotes_en'] = []
        self.assertFalse(self.guard(other))

    def test_reference_rewrite_rejected(self):
        other = copy.deepcopy(self.data)
        other['references'][0]['text'] = 'Different source.'
        self.assertFalse(self.guard(other))

    def test_reordering_and_code_edits_rejected(self):
        other = copy.deepcopy(self.data)
        other['blocks'][0]['rows'][0].reverse()
        self.assertFalse(self.guard(other))
        from easyread.repair_guard import retains_content
        self.assertFalse(retains_content({'zh': {'p1': '`x$`'}}, {'zh': {'p1': '`x`'}}))

    def test_invalid_response_types_rejected(self):
        for other in (None, [], 'text', 0):
            with self.subTest(other=other):
                self.assertFalse(self.guard(other))

    def test_currency_and_existing_delimiters_cannot_be_deleted(self):
        from easyread.repair_guard import retains_content
        self.assertFalse(retains_content({'zh': {'p1': 'Cost $20'}}, {'zh': {'p1': 'Cost 20'}}))
        self.assertFalse(retains_content({'zh': {'p1': '$x$'}}, {'zh': {'p1': r'\(x\)'}}))
        self.assertTrue(retains_content({'zh': {'p1': r'\alpha'}}, {'zh': {'p1': r'\(\alpha\)'}}))

    def test_fill_keys_and_nested_table_content_protected(self):
        from easyread.repair_guard import retains_content
        original = {'zh': {'t1': {'rows': [['2 mg', r'\alpha']]}}}
        self.assertFalse(retains_content(original, {'zh': {'other': 'full enough'}}))
        self.assertTrue(retains_content(original, {'zh': {'t1': {'rows': [['2 mg', r'$\alpha$']]}}}))


class RepairPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name))
        write_json_atomic(self.ws.paper_path, {'meta': {}, 'blocks': [
            {'id': 'p1', 'type': 'para', 'page': 1, 'en': 'Original English 2 mg.'}]})
        self.original = {'blocks': [{'id': 'p1', 'type': 'para', 'page': 1,
            'en': 'Original English 2 mg.', 'zh': r'數值 \alpha = 2 mg [1]。'}]}
        (self.ws.root / 'manual_table_links.json').write_bytes(b'preserve manual snapshot')
        self.before = {p.name: p.read_bytes() for p in self.ws.root.iterdir() if p.is_file()}

    def batch(self, fixed):
        with ExitStack() as stack:
            stack.enter_context(patch.object(translate.engines, 'image_mode', return_value='text'))
            stack.enter_context(patch.object(translate.prompts, 'translate', return_value='mock prompt'))
            stack.enter_context(patch.object(translate.engines, 'run', side_effect=[
                json.dumps(self.original), fixed if isinstance(fixed, Exception) else json.dumps(fixed)]))
            stack.enter_context(patch.object(translate, 'tex_problems', return_value=[]))
            for name in ('_unify_terms', '_save_checks'):
                stack.enter_context(patch.object(translate, name))
            stack.enter_context(patch.object(translate, '_one_references', return_value=[]))
            stack.enter_context(patch.object(translate.page_check, 'missing_pages', return_value=[]))
            stack.enter_context(patch.object(translate.pdfwork, 'locate'))
            merge = stack.enter_context(patch.object(translate, 'merge_blocks'))
            translate._one_batch(self.ws, {}, [1], 1, None, lambda *_: None)
            result = copy.deepcopy(merge.call_args.args[1])
        for name, raw in self.before.items():
            self.assertEqual((self.ws.root / name).read_bytes(), raw)
        return result

    def test_batch_repair_cannot_drop_or_rename_blocks(self):
        fixed = copy.deepcopy(self.original)
        fixed['blocks'][0].update(id='other', zh='short')
        self.assertEqual(self.batch(fixed), self.original)

    def test_batch_repair_cannot_change_statistics(self):
        fixed = copy.deepcopy(self.original)
        fixed['blocks'][0]['zh'] = r'數值 $\alpha = 3$ mg [1]。'
        self.assertEqual(self.batch(fixed), self.original)

    def test_batch_delimiter_repair_accepted(self):
        fixed = copy.deepcopy(self.original)
        fixed['blocks'][0]['zh'] = r'數值 $\alpha = 2$ mg [1]。'
        self.assertEqual(self.batch(fixed), fixed)

    def test_batch_malformed_or_failed_repair_falls_back(self):
        for fixed in (None, [], {'blocks': []}):
            with self.subTest(fixed=fixed):
                self.assertEqual(self.batch(fixed), self.original)

    def test_batch_model_error_falls_back(self):
        self.assertEqual(self.batch(translate.engines.EngineError('Mock failure')), self.original)

    def test_batch_cancelled_repair_does_not_merge(self):
        with self.assertRaises(translate.engines.Cancelled):
            self.batch(translate.engines.Cancelled())
        for name, raw in self.before.items():
            self.assertEqual((self.ws.root / name).read_bytes(), raw)

    def test_batch_invalid_tex_repair_still_falls_back(self):
        fixed = copy.deepcopy(self.original)
        fixed['blocks'][0]['zh'] = r'數值 $\alpha = 2$ mg [1]。'
        with patch.object(translate, 'block_problems', return_value=(['still invalid'], [])):
            self.assertEqual(self.batch(fixed), self.original)

    def test_fill_repair_cannot_replace_keys_or_shrink_text(self):
        original = {'zh': {'p1': r'數值 $\alpha = 2 mg [1]。'}}
        fixed = {'zh': {'p1': '短'}}
        with ExitStack() as stack:
            stack.enter_context(patch.object(translate.prompts_en, 'todo', return_value={'p1': 'English'}))
            stack.enter_context(patch.object(translate.sentences, 'mark_items', return_value=({'p1': 'English'}, {})))
            stack.enter_context(patch.object(translate.prompts_en, 'fill', return_value='mock prompt'))
            stack.enter_context(patch.object(translate.engines, 'run', side_effect=[json.dumps(original), json.dumps(fixed)]))
            stack.enter_context(patch.object(translate, 'tex_problems', return_value=[]))
            stack.enter_context(patch.object(translate, '_unify_terms'))
            stack.enter_context(patch.object(translate, '_save_checks'))
            stack.enter_context(patch.object(translate.sentences, 'unmark_fill', return_value={}))
            fill = stack.enter_context(patch.object(translate, 'fill_zh', return_value=[]))
            translate._fill_batch(self.ws, {}, [1], None, lambda *_: None)
            self.assertEqual(fill.call_args.args[1], original)


class LinkedRepairIntegrationTest(unittest.TestCase):
    def test_real_fill_with_corrupt_neighbor_preserves_confirmed_table_and_history(self):
        from tests.test_manual_table_links import ManualTableLinksTest
        from easyread import manual_table_links as links, reader_data
        from easyread.library import Library
        fixture = ManualTableLinksTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ws, record = fixture.prepare()
        links.handle(ws, fixture.request(ws, record))
        ws.update('paper', lambda p: p['blocks'].append({
            'id': 'newpara', 'type': 'para', 'page': 2, 'en': 'Value 2 mg.'}))
        old_blocks = copy.deepcopy(ws.load('paper')['blocks'][:-1])
        protected = {p: p.read_bytes() for p in ws.root.rglob('*')
                     if p.is_file() and p != ws.paper_path}
        bad = ws.root.parent / 'bad00001'
        bad.mkdir()
        write_json_atomic(bad / 'paper.json', {})
        (bad / 'reader.json').write_bytes(b'{broken')
        entries = {i['id']: i for i in Library(ws.root.parent).list()}
        self.assertIn('load_error', entries['bad00001'])
        self.assertNotIn('load_error', entries[ws.id])
        first = {'zh': {'newpara': r'數值 \alpha = 2 mg [1]。'}}
        fixed = {'zh': {'newpara': r'數值 $\alpha = 2$ mg [1]。'}}
        with patch.object(translate.prompts_en, 'fill', return_value='mock'), \
                patch.object(translate.sentences, 'mark_items', return_value=({'newpara': 'Value 2 mg.'}, {})), \
                patch.object(translate.engines, 'run', side_effect=[json.dumps(first), json.dumps(fixed)]) as model, \
                patch.object(translate, 'tex_problems', return_value=[]):
            translate._fill_batch(ws, {}, [2], None, lambda *_: None)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(ws.load('paper')['blocks'][:-1], old_blocks)
        self.assertEqual(ws.load('paper')['blocks'][-1]['zh'], fixed['zh']['newpara'])
        self.assertEqual({p: p.read_bytes() for p in protected}, protected)
        with patch.object(translate.engines, 'run', side_effect=AssertionError('No model on reopen')):
            paper, _ = reader_data.reader_paper(Workspace(ws.root))
        view = next(b for b in paper['blocks'] if b['id'] == 'tab1')[links.VIEW]
        self.assertEqual(view['values'], record['table_reading_zh'])


if __name__ == '__main__':
    unittest.main()
