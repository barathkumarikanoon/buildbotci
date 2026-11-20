import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

import runner
from hub.store import Store

STUB = '''
import inspect
import os
import subprocess

PASSED = 'passed'
FAILED = 'failed'
ERROR = 'error'
NO_EXPECTED = 'no_expected'


class Alpha:
    def one(self, outhandle):
        outhandle.write('1')

    def two(self):
        return True


MODULES = {'alpha': (Alpha, lambda storage, srcs: Alpha())}


def output_filename(name):
    return name


def get_class_tests(cls):
    return [n for n, m in inspect.getmembers(cls, inspect.isfunction) if not n.startswith('_')]
'''


def git(cwd, *args):
    done = subprocess.run(['git', '-C', cwd] + list(args), stdout = subprocess.PIPE, stderr = subprocess.PIPE)
    if done.returncode != 0:
        raise AssertionError('git %s failed: %s' % (' '.join(args), done.stderr.decode()))
    return done.stdout.decode().strip()


class GitFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp     = tempfile.TemporaryDirectory()
        base         = self.tmp.name
        self.origin  = os.path.join(base, 'origin.git')
        self.pusher  = os.path.join(base, 'pusher')
        self.source  = os.path.join(base, 'projects', 'indianlaw')
        self.results = os.path.join(base, 'results')
        git(base, 'init', '-q', '--bare', '-b', 'trunk', self.origin)
        git(base, 'clone', '-q', self.origin, self.pusher)
        for key, value in (('user.name', 't'), ('user.email', 't@t')):
            git(self.pusher, 'config', key, value)
        os.makedirs(os.path.join(self.pusher, 'regressions'))
        with open(os.path.join(self.pusher, 'regressions', 'regression.py'), 'w') as handle:
            handle.write(STUB)
        self.first = self.push('first')
        os.makedirs(os.path.dirname(self.source))
        git(base, 'clone', '-q', self.origin, self.source)
        with open(os.path.join(self.source, 'WORK_IN_PROGRESS'), 'w') as handle:
            handle.write('not committed')
        self.saved_repo = runner.REPO

    def tearDown(self):
        runner.REPO = self.saved_repo
        self.tmp.cleanup()

    def push(self, text, branch = 'trunk'):
        with open(os.path.join(self.pusher, 'notes.txt'), 'a') as handle:
            handle.write(text + '\n')
        git(self.pusher, 'checkout', '-q', '-B', branch)
        git(self.pusher, 'add', '-A')
        git(self.pusher, 'commit', '-q', '-m', text)
        git(self.pusher, 'push', '-q', 'origin', branch)
        return git(self.pusher, 'rev-parse', 'HEAD')

    def args(self, **values):
        base = dict(repo_dir = self.source, results_dir = self.results, branch = 'trunk', trigger = '')
        base.update(values)
        return argparse.Namespace(**base)

    def call(self, command, **values):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = getattr(runner, command)(self.args(**values))
        lines = {}
        for line in out.getvalue().splitlines():
            key, sep, value = line.partition(': ')
            if sep and key.isupper():
                lines[key] = value
        return code, lines, out.getvalue()

    def test_a_manual_sync_fetches_origin_and_always_runs_the_latest_commit(self):
        newest = self.push('second')
        code, found, _ = self.call('cmd_sync', trigger = 'manual')
        self.assertEqual((code, found['ACTION'], found['REVISION']), (0, 'run', newest))
        self.assertEqual(self.call('cmd_sync', trigger = 'manual')[1]['ACTION'], 'run')

    def test_a_scheduled_sync_runs_once_per_new_commit(self):
        code, found, _ = self.call('cmd_sync', trigger = 'push')
        self.assertEqual((found['ACTION'], found['REVISION']), ('run', self.first))
        code, found, text = self.call('cmd_sync', trigger = 'push')
        self.assertEqual(found['ACTION'], 'skip')
        self.assertIn('Nothing to run', text)
        newest = self.push('second')
        code, found, _ = self.call('cmd_sync', trigger = 'push')
        self.assertEqual((found['ACTION'], found['REVISION']), ('run', newest))
        self.assertEqual(Store(self.results).last_tested('trunk')['revision'], newest)

    def test_branches_are_tracked_separately(self):
        self.push('other branch work', branch = 'release')
        self.assertEqual(self.call('cmd_sync', trigger = 'push')[1]['ACTION'], 'run')
        self.assertEqual(self.call('cmd_sync', trigger = 'push', branch = 'release')[1]['ACTION'], 'run')
        self.assertEqual(self.call('cmd_sync', trigger = 'push')[1]['ACTION'], 'skip')
        self.assertEqual(self.call('cmd_sync', trigger = 'push', branch = 'release')[1]['ACTION'], 'skip')

    def test_sync_never_touches_the_working_copy(self):
        head = git(self.source, 'rev-parse', 'HEAD')
        self.push('second')
        self.call('cmd_sync', trigger = 'push')
        self.assertEqual(git(self.source, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.source, 'branch', '--show-current'), 'trunk')
        self.assertTrue(os.path.exists(os.path.join(self.source, 'WORK_IN_PROGRESS')))
        self.assertNotEqual(git(self.source, 'rev-parse', 'origin/trunk'), head)

    def test_sync_fails_loudly_for_a_branch_that_is_not_on_origin(self):
        with self.assertRaises(SystemExit) as caught:
            self.call('cmd_sync', branch = 'nope')
        self.assertIn('nope', str(caught.exception))

    def test_checkout_makes_an_isolated_tree_and_release_removes_it(self):
        newest = self.push('second')
        dest = os.path.join(self.tmp.name, 'work', 'run-1', 'indianlaw')
        self.call('cmd_checkout', dest = dest, revision = newest)
        self.assertEqual(git(dest, 'rev-parse', 'HEAD'), newest)
        self.assertTrue(os.path.exists(os.path.join(dest, 'regressions', 'regression.py')))
        self.assertFalse(os.path.exists(os.path.join(dest, 'WORK_IN_PROGRESS')))
        self.assertIn(dest, git(self.source, 'worktree', 'list'))
        self.call('cmd_release', dest = dest)
        self.assertNotIn(dest, git(self.source, 'worktree', 'list'))
        self.assertFalse(os.path.exists(dest))
        self.assertTrue(os.path.exists(os.path.join(self.source, 'WORK_IN_PROGRESS')))

    def test_the_same_folder_can_be_moved_to_another_commit(self):
        newest = self.push('second')
        dest = os.path.join(self.tmp.name, 'work', 'indianlaw')
        self.call('cmd_checkout', dest = dest, revision = self.first)
        with open(os.path.join(dest, 'stray'), 'w') as handle:
            handle.write('left by a test')
        self.call('cmd_checkout', dest = dest, revision = newest)
        self.assertEqual(git(dest, 'rev-parse', 'HEAD'), newest)
        self.assertFalse(os.path.exists(os.path.join(dest, 'stray')))

    def test_checkout_fetches_a_commit_it_does_not_have_yet(self):
        newest = self.push('pushed after the last fetch')
        self.assertNotEqual(subprocess.run(['git', '-C', self.source, 'cat-file', '-e', newest]).returncode, 0)
        dest = os.path.join(self.tmp.name, 'work', 'indianlaw')
        self.call('cmd_checkout', dest = dest, revision = newest)
        self.assertEqual(git(dest, 'rev-parse', 'HEAD'), newest)

    def test_checkout_folder_must_be_called_indianlaw(self):
        with self.assertRaises(SystemExit):
            self.call('cmd_checkout', dest = os.path.join(self.tmp.name, 'work', 'oops'), revision = self.first)

    def test_a_folder_that_is_not_a_git_repository_is_refused(self):
        plain = os.path.join(self.tmp.name, 'plain', 'indianlaw')
        os.makedirs(plain)
        with self.assertRaises(SystemExit) as caught:
            self.call('cmd_sync', repo_dir = plain)
        self.assertIn('Not a git repository', str(caught.exception))

    def run_catalog(self, branch = 'trunk'):
        return subprocess.run([sys.executable, '-W', 'ignore', os.path.join(runner.HERE, 'runner.py'),
                               '--repo-dir', self.source, '--results-dir', self.results, 'catalog', '--branch', branch],
                              stdout = subprocess.PIPE, stderr = subprocess.PIPE, cwd = self.tmp.name)

    def test_a_failed_catalog_records_why_and_a_good_one_clears_it(self):
        with open(os.path.join(self.pusher, 'regressions', 'regression.py'), 'w') as handle:
            handle.write('PASSED = "passed"\n')
        git(self.pusher, 'add', '-A')
        git(self.pusher, 'commit', '-q', '-m', 'old regression.py without the module list')
        git(self.pusher, 'push', '-q', 'origin', 'trunk')
        done = self.run_catalog()
        self.assertNotEqual(done.returncode, 0)
        store = Store(self.results)
        self.assertIn('regression.MODULES', store.catalog_error('trunk')['message'])
        self.assertEqual(store.catalog('trunk')['tests'], [])
        with open(os.path.join(self.pusher, 'regressions', 'regression.py'), 'w') as handle:
            handle.write(STUB)
        git(self.pusher, 'commit', '-q', '-am', 'add the module list')
        git(self.pusher, 'push', '-q', 'origin', 'trunk')
        self.assertEqual(self.run_catalog().returncode, 0)
        self.assertIsNone(store.catalog_error('trunk'))
        self.assertEqual(len(store.catalog('trunk')['tests']), 2)

    def test_an_unknown_branch_is_recorded_as_a_failed_refresh(self):
        self.assertNotEqual(self.run_catalog('nope').returncode, 0)
        self.assertIn('nope', Store(self.results).catalog_error('nope')['message'])

    def test_the_catalog_is_read_from_origin_in_a_clean_process_and_leaves_nothing_behind(self):
        newest = self.push('second')
        done = subprocess.run([sys.executable, '-W', 'ignore', os.path.join(runner.HERE, 'runner.py'),
                               '--repo-dir', self.source, '--results-dir', self.results, 'catalog', '--branch', 'trunk'],
                              stdout = subprocess.PIPE, stderr = subprocess.PIPE, cwd = self.tmp.name)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        catalog = Store(self.results).catalog('trunk')
        self.assertEqual(catalog['revision'], newest)
        self.assertEqual(sorted(t['id'] for t in catalog['tests']), ['alpha.one', 'alpha.two'])
        self.assertEqual(git(self.source, 'worktree', 'list').count('\n'), 0)
        self.assertTrue(os.path.exists(os.path.join(self.source, 'WORK_IN_PROGRESS')))


if __name__ == '__main__':
    unittest.main()
