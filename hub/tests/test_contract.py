import contextlib
import io
import os
import tempfile
import unittest

from hub import repos
from hub.adapters.comparison import OutputComparisonAdapter
from hub.tests import app_standin, fixtures

REPO_DIR = repos.expand_path(os.environ.get('CI_REPO_DIR') or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '..', 'indianlaw'))


class RegressionContractTest(unittest.TestCase):
    def setUp(self):
        if not os.path.isdir(REPO_DIR):
            self.skipTest('no code checkout at %s; set CI_REPO_DIR to run the contract tests' % REPO_DIR)
        self.cwd = os.getcwd()
        config = fixtures.repo_config(os.path.dirname(REPO_DIR), repo_dir = REPO_DIR)
        self.adapter = OutputComparisonAdapter(config, REPO_DIR, config.options)
        self.adapter.activate()
        try:
            import regression
        except ImportError as error:
            os.chdir(self.cwd)
            self.skipTest('the code at %s cannot be imported by this Python (%s); run the tests with the Python of its '
                          'environment (the Python of its repo profile)' % (REPO_DIR, error))
        if not hasattr(regression, 'MODULES'):
            os.chdir(self.cwd)
            self.skipTest('the code at %s has no regression.MODULES yet: the regression.py hook is not on this branch' % REPO_DIR)
        self.regression = regression
        self.tmp = tempfile.TemporaryDirectory()
        fixtures.write_expected(self.tmp.name)
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def evaluate(self, checklist, populate = False):
        objdict = {'fake': fixtures.Fake(), 'other': fixtures.Other()}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            failures = self.regression.evaluate(checklist, objdict, populate)
        return failures, out.getvalue()

    def test_counts_every_kind_of_failure(self):
        failures, text = self.evaluate(['same', 'differs', 'nobaseline', 'truthy', 'falsy', 'only'])
        self.assertEqual(failures, 3)
        self.assertIn('Regression for same ... OK', text)
        self.assertIn('Regression for differs  ... Failed', text)
        self.assertIn('Missing expected/nobaseline', text)
        self.assertTrue(os.path.exists('diffs/differs.diff'))

    def test_unknown_test_name_is_a_failure(self):
        failures, text = self.evaluate(['same', 'nonexistent'])
        self.assertEqual(failures, 1)
        self.assertIn('No such test', text)

    def test_populate_writes_expected_and_reports_no_failure(self):
        failures, _ = self.evaluate(['nobaseline'], populate = True)
        self.assertEqual(failures, 0)
        with open('expected/nobaseline') as handle:
            self.assertEqual(handle.read(), 'fresh output')
        failures, _ = self.evaluate(['nobaseline'])
        self.assertEqual(failures, 0)

    def test_output_filename_suffixes(self):
        self.assertEqual(self.regression.output_filename('resolve_acts'), 'resolve_acts.csv')
        self.assertEqual(self.regression.output_filename('act_extract'), 'act_extract.xml')
        self.assertEqual(self.regression.output_filename('cite_law'), 'cite_law.txt')
        self.assertEqual(self.regression.output_filename('bombay_hdr'), 'bombay_hdr')

    def test_registry_matches_every_module_the_old_loader_knew(self):
        self.assertEqual(sorted(self.regression.MODULES), sorted([
            'search', 'causelist', 'header', 'structural', 'layout', 'cite_extract',
            'commonfuncs', 'resolution', 'enrichment', 'header_words', 'serialization',
            'loksabha', 'metadb', 'causelists', 'casealert', 'lawsik', 'citetext', 'citelaw']))

    def test_every_module_is_a_class_and_a_factory(self):
        for name, entry in self.regression.MODULES.items():
            cls, factory = entry
            self.assertTrue(isinstance(cls, type), name)
            self.assertTrue(callable(factory), name)
            self.assertTrue(self.regression.get_class_tests(cls), name)

    def test_output_naming_and_comparison_match_what_the_ci_tests_assume(self):
        for name in ('resolve_acts', 'judge_cite_extract', 'cite_text', 'cite_law', 'legal_term_extract', 'bombay_hdr'):
            self.assertEqual(self.regression.output_filename(name), app_standin.output_filename(name), name)
        for name in ('PASSED', 'FAILED', 'ERROR', 'NO_EXPECTED'):
            self.assertEqual(getattr(self.regression, name), getattr(app_standin, name))
        left, right, missing = [os.path.join(self.tmp.name, n) for n in ('l', 'r', 'none')]
        with open(left, 'w') as handle:
            handle.write('a\nb\n')
        with open(right, 'w') as handle:
            handle.write('a\nc\n')
        for pair in ((left, left), (left, right), (missing, right)):
            self.assertEqual(self.regression.compare_files(*pair), app_standin.compare_files(*pair))

    def test_the_catalog_the_ci_builds_is_complete(self):
        catalog = self.adapter.catalog()
        self.assertEqual(sorted(catalog['modules']), sorted(self.regression.MODULES))
        self.assertTrue(all(t['module'] in catalog['modules'] for t in catalog['tests']))
        self.assertGreater(len(catalog['tests']), len(catalog['modules']))

    def test_unknown_module_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.regression.load_regress_objs(None, [], ['nope'])


if __name__ == '__main__':
    unittest.main()
