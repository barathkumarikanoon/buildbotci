import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

from hub import gitrepo, repos
from hub.commands import vcs
from hub.gitrepo import GitRepo
from hub.store import Store
from hub.tests import fixtures

CI_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
        self.config   = fixtures.repo_config(base, repo_dir = self.source)
        self.plain    = os.path.join(base, 'plain', 'indianlaw')
        self.profiles = os.path.join(self.results, repos.SNAPSHOT_NAME)
        repos.save_snapshot(self.profiles, [self.config, fixtures.repo_config(base, key = 'plain', repo_dir = self.plain)])
        self.store = Store(os.path.join(self.results, 'indianlaw'))

    def tearDown(self):
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
        base = dict(repo = 'indianlaw', profiles_file = self.profiles, tree = None, results_dir = self.results, branch = 'trunk', trigger = '')
        base.update(values)
        return argparse.Namespace(**base)

    def call(self, command, **values):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = getattr(vcs, command)(self.args(**values))
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
        self.assertEqual(self.store.last_tested('trunk')['revision'], newest)

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

    def make_trees(self, *names):
        base = os.path.join(self.tmp.name, 'workers')
        revision = git(self.source, 'rev-parse', 'HEAD')
        paths = {}
        for name in names:
            paths[name] = os.path.join(base, name, 'indianlaw')
            self.call('cmd_checkout', dest = paths[name], revision = revision)
        return base, paths

    def test_a_sweep_removes_the_checkouts_nobody_is_running_and_keeps_the_others(self):
        base, tree = self.make_trees('w1/indianlaw-run/run-3', 'w1/indianlaw-run/run-4',
                                     'w1/indianlaw-shard', 'w2/indianlaw-shard')
        os.makedirs(os.path.join(base, 'w1', 'indianlaw-run', 'run-5'))
        other = os.path.join(self.tmp.name, 'mine', 'indianlaw')
        self.call('cmd_checkout', dest = other, revision = git(self.source, 'rev-parse', 'HEAD'))
        busy = {('w1', 'indianlaw-run', 4), ('w2', 'indianlaw-shard', None)}
        removed = GitRepo(self.config).sweep(base, busy)
        self.assertEqual(sorted(os.path.relpath(r, base) for r in removed),
                         ['w1/indianlaw-run/run-3/indianlaw', 'w1/indianlaw-run/run-5/indianlaw', 'w1/indianlaw-shard/indianlaw'])
        self.assertFalse(os.path.exists(os.path.join(base, 'w1', 'indianlaw-run', 'run-5')))
        for gone in ('w1/indianlaw-run/run-3', 'w1/indianlaw-shard'):
            self.assertFalse(os.path.exists(tree[gone]))
        self.assertFalse(os.path.exists(os.path.join(base, 'w1', 'indianlaw-run', 'run-3')))
        self.assertTrue(os.path.exists(tree['w1/indianlaw-run/run-4']))
        self.assertTrue(os.path.exists(tree['w2/indianlaw-shard']))
        self.assertTrue(os.path.exists(other))
        listing = git(self.source, 'worktree', 'list')
        self.assertIn(tree['w1/indianlaw-run/run-4'], listing)
        self.assertNotIn(tree['w1/indianlaw-run/run-3'], listing)
        self.assertTrue(os.path.exists(os.path.join(self.source, 'WORK_IN_PROGRESS')))

    def test_a_dry_run_lists_but_removes_nothing(self):
        base, tree = self.make_trees('w1/indianlaw-run/run-3')
        code, _, out = self.call('cmd_sweep', base = base, buildbot_url = '', all = True, dry_run = True)
        self.assertIn('would remove old checkout', out)
        self.assertIn('1 old checkout to remove', out)
        self.assertTrue(os.path.exists(tree['w1/indianlaw-run/run-3']))

    def test_a_sweep_with_nothing_running_clears_everything_of_ours(self):
        base, tree = self.make_trees('w1/indianlaw-run/run-3', 'w2/indianlaw-shard')
        other = os.path.join(self.tmp.name, 'mine', 'indianlaw')
        self.call('cmd_checkout', dest = other, revision = git(self.source, 'rev-parse', 'HEAD'))
        self.assertEqual(len(GitRepo(self.config).sweep(base, set())), 2)
        self.assertEqual(os.listdir(os.path.join(base, 'w1')), ['indianlaw-run'])
        self.assertEqual(os.listdir(os.path.join(base, 'w1', 'indianlaw-run')), [])
        self.assertTrue(os.path.exists(other))
        self.assertEqual(GitRepo(self.config).sweep(base, set()), [])

    def test_a_checkout_whose_folder_was_deleted_by_hand_is_cleaned_from_gits_list(self):
        base, tree = self.make_trees('w1/indianlaw-run/run-3')
        import shutil
        shutil.rmtree(tree['w1/indianlaw-run/run-3'])
        self.assertIn(tree['w1/indianlaw-run/run-3'], git(self.source, 'worktree', 'list'))
        GitRepo(self.config).sweep(base, set())
        self.assertNotIn(tree['w1/indianlaw-run/run-3'], git(self.source, 'worktree', 'list'))

    def test_a_folder_git_does_not_list_is_still_removed(self):
        stray = os.path.join(self.tmp.name, 'stray', 'indianlaw')
        os.makedirs(os.path.join(stray, 'sub'))
        git = GitRepo(self.config)
        git.remove_worktree(stray)
        self.assertFalse(os.path.exists(stray))
        git.remove_worktree(stray)

    def test_the_code_checkout_itself_is_never_removed(self):
        with self.assertRaises(SystemExit):
            GitRepo(self.config).remove_worktree(self.source)
        self.assertTrue(os.path.exists(os.path.join(self.source, 'WORK_IN_PROGRESS')))

    def test_the_sweep_command_removes_nothing_when_buildbot_cannot_be_asked(self):
        base, tree = self.make_trees('w1/indianlaw-run/run-3')
        from unittest import mock
        with mock.patch.object(gitrepo, 'buildbot_in_use', return_value = None):
            code, _, out = self.call('cmd_sweep', base = base, buildbot_url = 'http://nowhere', all = False, dry_run = False)
        self.assertEqual(code, 0)
        self.assertIn('no checkout removed', out)
        self.assertTrue(os.path.exists(tree['w1/indianlaw-run/run-3']))

    def test_the_sweep_command_with_all_clears_even_if_buildbot_is_down(self):
        base, tree = self.make_trees('w1/indianlaw-run/run-3', 'w1/indianlaw-shard')
        code, _, out = self.call('cmd_sweep', base = base, buildbot_url = 'http://nowhere', all = True, dry_run = False)
        self.assertIn('2 old checkouts removed', out)
        self.assertFalse(os.path.exists(tree['w1/indianlaw-shard']))

    def test_buildbot_in_use_reads_the_running_builds(self):
        import io, json
        from unittest import mock
        answers = {'workers': {'workers': [{'workerid': 1, 'name': 'w1'}]},
                   'builders': {'builders': [{'builderid': 1, 'name': 'indianlaw-run'}, {'builderid': 2, 'name': 'indianlaw-shard'}]},
                   'builds?complete__eq=false': {'builds': [{'builderid': 1, 'workerid': 1, 'number': 7},
                                                            {'builderid': 2, 'workerid': 1, 'number': 3}]}}
        def fake(url, timeout = 0):
            return io.StringIO(json.dumps(answers[url.split('/api/v2/')[1]]))
        with mock.patch('urllib.request.urlopen', fake):
            self.assertEqual(gitrepo.buildbot_in_use('http://bb/'), {('w1', 'indianlaw-run', 7), ('w1', 'indianlaw-shard', None)})
        with mock.patch('urllib.request.urlopen', side_effect = OSError):
            self.assertIsNone(gitrepo.buildbot_in_use('http://bb/'))

    def test_checkout_folder_must_be_called_indianlaw(self):
        with self.assertRaises(SystemExit):
            self.call('cmd_checkout', dest = os.path.join(self.tmp.name, 'work', 'oops'), revision = self.first)

    def test_a_folder_that_is_not_a_git_repository_is_refused(self):
        os.makedirs(self.plain)
        with self.assertRaises(SystemExit) as caught:
            self.call('cmd_sync', repo = 'plain')
        self.assertIn('Not a git repository', str(caught.exception))

    def run_catalog(self, branch = 'trunk'):
        return subprocess.run([sys.executable, '-W', 'ignore', os.path.join(CI_DIR, 'runner.py'), '--repo', 'indianlaw',
                               '--profiles-file', self.profiles, '--results-dir', self.results, 'catalog', '--branch', branch],
                              stdout = subprocess.PIPE, stderr = subprocess.PIPE, cwd = self.tmp.name)

    def test_a_failed_catalog_records_why_and_a_good_one_clears_it(self):
        with open(os.path.join(self.pusher, 'regressions', 'regression.py'), 'w') as handle:
            handle.write('PASSED = "passed"\n')
        git(self.pusher, 'add', '-A')
        git(self.pusher, 'commit', '-q', '-m', 'old regression.py without the module list')
        git(self.pusher, 'push', '-q', 'origin', 'trunk')
        done = self.run_catalog()
        self.assertNotEqual(done.returncode, 0)
        store = self.store
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
        self.assertIn('nope', self.store.catalog_error('nope')['message'])

    def test_the_catalog_is_read_from_origin_in_a_clean_process_and_leaves_nothing_behind(self):
        newest = self.push('second')
        done = self.run_catalog('trunk')
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        catalog = self.store.catalog('trunk')
        self.assertEqual(catalog['revision'], newest)
        self.assertEqual(sorted(t['id'] for t in catalog['tests']), ['alpha.one', 'alpha.two'])
        self.assertEqual(git(self.source, 'worktree', 'list').count('\n'), 0)
        self.assertTrue(os.path.exists(os.path.join(self.source, 'WORK_IN_PROGRESS')))


if __name__ == '__main__':
    unittest.main()
