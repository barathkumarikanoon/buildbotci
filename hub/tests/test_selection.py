import os
import subprocess
import tempfile
import unittest

from hub import selection

CATALOG = {
    'modules': {
        'header':     {'class': 'H', 'source': 'regressions/judge_regressions.py'},
        'structural': {'class': 'S', 'source': 'regressions/judge_regressions.py'},
        'search':     {'class': 'Q', 'source': 'regressions/pylaw_regressions.py'},
    },
    'tests': [
        {'id': 'header.bombay_hdr', 'module': 'header', 'name': 'bombay_hdr', 'output': 'bombay_hdr'},
        {'id': 'header.delhi_hdr', 'module': 'header', 'name': 'delhi_hdr', 'output': 'delhi_hdr'},
        {'id': 'structural.bombay_structural', 'module': 'structural', 'name': 'bombay_structural', 'output': 'bombay_structural'},
        {'id': 'search.parse', 'module': 'search', 'name': 'parse', 'output': 'parse'},
    ],
}

IMPACT = {
    'unmapped': 'full',
    'ignore': ['*.md', 'docs/*'],
    'full': ['model/*'],
    'rules': [
        {'name': 'court', 'paths': ['judgments/*.py'], 'tests': ['*.{name}*'],
         'aliases': {'newdelhi': ['delhi']}, 'fallback_modules': ['header', 'structural']},
        {'name': 'search', 'paths': ['website/pylaw/search/*'], 'modules': ['search']},
    ],
}


class ExpandTest(unittest.TestCase):
    def test_module_id_name_and_glob(self):
        ids, missing = selection.expand_specs(['search', 'header.bombay_hdr', '*.delhi*', 'nope'], CATALOG)
        self.assertEqual(ids, ['header.bombay_hdr', 'header.delhi_hdr', 'search.parse'])
        self.assertEqual(missing, ['nope'])

    def test_all(self):
        self.assertEqual(len(selection.expand_specs(['all'], CATALOG)[0]), 4)

    def test_split(self):
        self.assertEqual(selection.split_specs('a b,c  d'), ['a', 'b', 'c', 'd'])


class ImpactTest(unittest.TestCase):
    def run_for(self, files):
        return selection.impacted(files, IMPACT, CATALOG)

    def test_court_file_selects_its_tests(self):
        r = self.run_for(['judgments/bombay.py'])
        self.assertEqual(r['tests'], ['header.bombay_hdr', 'structural.bombay_structural'])
        self.assertFalse(r['full'])

    def test_alias(self):
        self.assertEqual(self.run_for(['judgments/newdelhi.py'])['tests'], ['header.delhi_hdr'])

    def test_no_match_uses_fallback_modules(self):
        r = self.run_for(['judgments/court.py'])
        self.assertEqual(len(r['tests']), 3)

    def test_ignored_files_select_nothing(self):
        r = self.run_for(['README.md', 'docs/x.cfg'])
        self.assertEqual(r['tests'], [])
        self.assertEqual(len(r['ignored']), 2)

    def test_unmapped_forces_full(self):
        r = self.run_for(['utils/mail.py'])
        self.assertTrue(r['full'])
        self.assertEqual(len(r['tests']), 4)
        self.assertEqual(r['unmapped'], ['utils/mail.py'])

    def test_full_pattern(self):
        self.assertTrue(self.run_for(['model/doc.py'])['full'])

    def test_expected_file_selects_owner(self):
        r = self.run_for(['regressions/expected/parse'])
        self.assertEqual(r['tests'], ['search.parse'])

    def test_regression_module_source_selects_its_modules(self):
        r = self.run_for(['regressions/judge_regressions.py'])
        self.assertEqual(len(r['tests']), 3)

    def test_reasons_are_recorded(self):
        r = self.run_for(['website/pylaw/search/main_query.py'])
        self.assertIn('search (website/pylaw/search/main_query.py)', r['reasons']['search.parse'])


class GitTest(unittest.TestCase):
    def git(self, *args):
        subprocess.run(['git', '-C', self.repo] + list(args), check = True,
                       stdout = subprocess.PIPE, stderr = subprocess.PIPE)

    def commit(self, name, text = 'x'):
        with open(os.path.join(self.repo, name), 'w') as handle:
            handle.write(text)
        self.git('add', name)
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-m', name)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        self.git('init', '-q')
        self.commit('a.py')
        self.first = selection.head_revision(self.repo)
        self.commit('b.py')
        self.commit('c.py')

    def tearDown(self):
        self.tmp.cleanup()

    def test_changes_since_last_tested(self):
        base = selection.resolve_base(self.repo, self.first)
        self.assertEqual(base, self.first)
        self.assertEqual(selection.changed_files(self.repo, base), ['b.py', 'c.py'])

    def test_no_history_falls_back_to_parent(self):
        base = selection.resolve_base(self.repo, None)
        self.assertEqual(selection.changed_files(self.repo, base), ['c.py'])

    def test_unknown_last_tested_falls_back_to_parent(self):
        base = selection.resolve_base(self.repo, 'f' * 40)
        self.assertEqual(selection.changed_files(self.repo, base), ['c.py'])


if __name__ == '__main__':
    unittest.main()
