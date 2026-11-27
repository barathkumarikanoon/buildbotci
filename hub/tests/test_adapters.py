import contextlib
import io
import os
import sys
import tempfile
import unittest

from hub.adapters import ADAPTERS, adapter_class, make_adapter
from hub.adapters.command import CommandAdapter, parse_checks
from hub.adapters.pytest_suite import test_id
from hub.execution import execute_test
from hub.tests import fixtures


def read(path):
    with open(path) as handle:
        return handle.read()


def repo_with(kind, options, root):
    return fixtures.repo_config(root, key = kind, adapter = kind, options = options, python = sys.executable,
                                repo_dir = os.path.join(root, 'clone'))


class RegistryTest(unittest.TestCase):
    def test_the_three_kinds_of_suite_are_known(self):
        self.assertEqual(sorted(ADAPTERS), ['command', 'comparison', 'pytest'])

    def test_an_unknown_kind_is_explained(self):
        with self.assertRaises(SystemExit) as caught:
            adapter_class('nope')
        self.assertIn('command', str(caught.exception))

    def test_every_adapter_describes_itself_for_the_form(self):
        for kind, cls in ADAPTERS.items():
            self.assertEqual(cls.kind, kind)
            self.assertTrue(cls.label and cls.summary, kind)
            self.assertEqual(len({f.name for f in cls.fields}), len(cls.fields), kind)


class OptionsTest(unittest.TestCase):
    def test_defaults_fill_what_is_missing_and_unknown_settings_are_dropped(self):
        options = CommandAdapter.with_defaults({'compare': 'golden', 'bogus': 1})
        self.assertEqual((options['compare'], options['cwd'], options['timeout']), ('golden', '.', 1800))
        self.assertNotIn('bogus', options)

    def test_required_numbers_and_choices_are_checked(self):
        options, problems = CommandAdapter.clean_options({'checks': '', 'compare': 'magic', 'timeout': 'ten', 'expected_exit': '0'})
        self.assertEqual(sorted(problems), ['checks', 'compare', 'timeout'])
        options, problems = CommandAdapter.clean_options({'checks': 'a | true', 'compare': 'exit', 'timeout': '60', 'expected_exit': '2'})
        self.assertEqual(problems, {})
        self.assertEqual((options['timeout'], options['expected_exit']), (60, 2))

    def test_folders_cannot_leave_the_checkout(self):
        _, problems = CommandAdapter.clean_options({'checks': 'a | true', 'cwd': '../x'})
        self.assertIn('cwd', problems)
        _, problems = CommandAdapter.clean_options({'checks': 'a | true', 'golden_dir': '/etc'})
        self.assertIn('golden_dir', problems)

    def test_a_bad_check_line_is_reported_on_the_checks_field(self):
        _, problems = CommandAdapter.clean_options({'checks': 'no separator here'})
        self.assertIn('line 1', problems['checks'])

    def test_indianlaw_needs_its_environment_profile(self):
        _, problems = ADAPTERS['comparison'].clean_options({'env_profile': ''})
        self.assertIn('env_profile', problems)


class ParseChecksTest(unittest.TestCase):
    def test_lines_make_checks(self):
        self.assertEqual(parse_checks('# c\nsmoke.import | python -c "a | b"\n\nlint | ruff .\n'),
                         [('smoke.import', 'python -c "a | b"'), ('lint', 'ruff .')])

    def test_bad_lines_and_duplicates_are_refused(self):
        for text in ('onlyname', '| cmd', 'name |', 'bad name | x', '../x | y', 'a | x\na | y'):
            with self.assertRaises(ValueError, msg = text):
                parse_checks(text)


class CommandSuiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = os.path.join(self.tmp.name, 'clone')
        os.makedirs(os.path.join(self.tree, 'expected'))
        with open(os.path.join(self.tree, 'expected', 'g.same.txt'), 'w') as handle:
            handle.write('hello\nworld\n')
        with open(os.path.join(self.tree, 'expected', 'g.differs.txt'), 'w') as handle:
            handle.write('one\ntwo\nthree\n')

    def adapter(self, **options):
        base = {'checks': 'smoke.ok | echo fine\nsmoke.bad | exit 3\nsmoke.slow | sleep 5', 'timeout': 1}
        base.update(options)
        return make_adapter(repo_with('command', base, self.tmp.name), self.tree)

    def run_one(self, adapter, test_id):
        entry = {t['id']: t for t in adapter.catalog()['tests']}[test_id]
        tdir = os.path.join(self.tmp.name, 'out', test_id)
        with contextlib.redirect_stdout(io.StringIO()):
            return execute_test(adapter, adapter.open(), entry, tdir), tdir

    def test_the_catalog_groups_checks_into_modules(self):
        catalog = self.adapter(checks = 'a.x | true\na.y | true\nplain | true').catalog()
        self.assertEqual(sorted(catalog['modules']), ['a', 'checks'])
        self.assertEqual([(t['id'], t['module'], t['name']) for t in catalog['tests']], [('a.x', 'a', 'x'), ('a.y', 'a', 'y'), ('plain', 'checks', 'plain')])

    def test_a_check_passes_on_the_expected_exit_code(self):
        result, tdir = self.run_one(self.adapter(), 'smoke.ok')
        self.assertEqual(result['status'], 'passed')
        self.assertIn('fine', read(os.path.join(tdir, 'log.txt')))
        self.assertFalse(os.path.exists(os.path.join(tdir, 'actual')))

    def test_another_exit_code_fails_and_keeps_the_output(self):
        result, tdir = self.run_one(self.adapter(), 'smoke.bad')
        self.assertEqual((result['status'], result['reason']), ('failed', 'Exited with code 3 instead of 0'))
        self.assertTrue(os.path.exists(os.path.join(tdir, 'actual')))

    def test_the_expected_exit_code_can_be_changed(self):
        result, _ = self.run_one(self.adapter(expected_exit = 3), 'smoke.bad')
        self.assertEqual(result['status'], 'passed')

    def test_a_check_that_takes_too_long_is_an_error_and_is_stopped(self):
        result, _ = self.run_one(self.adapter(), 'smoke.slow')
        self.assertEqual(result['status'], 'error')
        self.assertIn('Timed out', result['reason'])

    def test_checks_run_in_the_environment_of_the_repo(self):
        adapter = make_adapter(fixtures.repo_config(self.tmp.name, key = 'command', adapter = 'command', python = sys.executable,
                               repo_dir = self.tree, env = {'MY_SETTING': 'from-the-profile'},
                               options = {'checks': 'env | echo $MY_SETTING $CI_REPO'}), self.tree)
        result, tdir = self.run_one(adapter, 'env')
        self.assertIn('from-the-profile command', read(os.path.join(tdir, 'log.txt')))

    def golden(self, **kw):
        return self.adapter(compare = 'golden', checks = 'g.same | printf "hello\\nworld\\n"\ng.differs | printf "one\\nTWO\\nthree\\n"\ng.new | echo x', **kw)

    def test_golden_mode_passes_when_the_output_equals_the_file(self):
        result, _ = self.run_one(self.golden(), 'g.same')
        self.assertEqual(result['status'], 'passed')

    def test_golden_mode_shows_a_diff_like_a_diff_based_suite(self):
        result, tdir = self.run_one(self.golden(), 'g.differs')
        self.assertEqual((result['status'], result['diff']['hunks']), ('failed', 1))
        self.assertIn('+TWO', read(os.path.join(tdir, 'diff.txt')))
        self.assertEqual(read(os.path.join(tdir, 'expected')), 'one\ntwo\nthree\n')
        self.assertEqual(read(os.path.join(tdir, 'actual')), 'one\nTWO\nthree\n')

    def test_golden_mode_without_a_file_has_no_baseline_and_says_how_to_make_one(self):
        result, _ = self.run_one(self.golden(), 'g.new')
        self.assertEqual(result['status'], 'no_expected')
        self.assertIn('expected/g.new.txt', result['baseline'][0])

    def test_it_can_say_how_to_run_a_check_by_hand(self):
        result, _ = self.run_one(self.adapter(), 'smoke.ok')
        self.assertEqual(result['reproduce'], 'cd .\necho fine')


class PytestSuiteTest(unittest.TestCase):
    def test_ids_are_safe_folder_names_and_unique(self):
        taken = set()
        first = test_id('tests/test_chat.py::TestBot::test_a[x y/z]', taken)
        again = test_id('tests/test_chat.py::TestBot::test_a[x y/z]', taken)
        self.assertEqual(first[1:], ('tests.test_chat', 'TestBot.test_a_x_y_z_'))
        self.assertRegex(first[0], r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
        self.assertNotEqual(first[0], again[0])

    def test_very_long_names_are_shortened(self):
        ident, _, _ = test_id('tests/t.py::' + 'x' * 400, set())
        self.assertLessEqual(len(ident), 128)

    def test_collecting_and_running_real_pytest_tests(self):
        try:
            import pytest  # noqa: F401
        except ImportError:
            self.skipTest('pytest is not installed in this Python')
        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, 'clone')
            os.makedirs(os.path.join(tree, 'tests'))
            with open(os.path.join(tree, 'tests', 'test_a.py'), 'w') as handle:
                handle.write('def test_good():\n    assert 1\n\ndef test_bad():\n    assert 1 == 2, "nope"\n')
            adapter = make_adapter(repo_with('pytest', {'paths': 'tests'}, tmp), tree)
            catalog = adapter.catalog()
            self.assertEqual(sorted(t['id'] for t in catalog['tests']), ['tests.test_a.test_bad', 'tests.test_a.test_good'])
            statuses = {}
            for entry in catalog['tests']:
                with contextlib.redirect_stdout(io.StringIO()):
                    statuses[entry['id']] = execute_test(adapter, adapter.open(), entry, os.path.join(tmp, 'o', entry['id']))
            self.assertEqual(statuses['tests.test_a.test_good']['status'], 'passed')
            self.assertEqual(statuses['tests.test_a.test_bad']['status'], 'failed')
            self.assertIn('nope', statuses['tests.test_a.test_bad']['reason'])

    def test_a_suite_pytest_cannot_collect_is_an_error_with_the_reason(self):
        try:
            import pytest  # noqa: F401
        except ImportError:
            self.skipTest('pytest is not installed in this Python')
        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, 'clone')
            os.makedirs(os.path.join(tree, 'tests'))
            with open(os.path.join(tree, 'tests', 'test_broken.py'), 'w') as handle:
                handle.write('import module_that_does_not_exist\n')
            with self.assertRaises(RuntimeError) as caught:
                make_adapter(repo_with('pytest', {'paths': 'tests'}, tmp), tree).catalog()
            self.assertIn('could not list', str(caught.exception))


if __name__ == '__main__':
    unittest.main()


class SuiteTypeNamesTest(unittest.TestCase):
    def test_each_type_is_named_by_what_it_does(self):
        self.assertEqual({k: c.label for k, c in ADAPTERS.items()}, {
            'comparison': 'Output comparison', 'pytest': 'pytest (Python tests)', 'command': 'Command checks'})

    def test_the_old_name_still_leads_to_the_renamed_type(self):
        from hub import repos
        from hub.adapters import adapter_class
        from hub.tests import fixtures
        self.assertIs(adapter_class('indianlaw'), adapter_class('comparison'))
        old = repos.RepoConfig.from_dict(dict(fixtures.repo_config('/tmp').to_dict(), adapter = 'indianlaw'))
        self.assertEqual(old.adapter, 'comparison')
