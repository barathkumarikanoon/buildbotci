import argparse
import contextlib
import os
import sys
import types
from unittest import mock

from hub import repos
from hub.adapters.comparison import OutputComparisonAdapter
from hub.commands import runs
from hub.gitrepo import GitRepo
from hub.tests import app_standin

BASE_LINES = ['RELURL: doc/%d\nbody %d\n' % (i, i) for i in range(1, 21)]
EXPECTED   = ''.join(BASE_LINES)
CHANGED    = ''.join(BASE_LINES).replace('body 3\n', 'body THREE\n').replace('body 18\n', 'body <b>EIGHTEEN</b>\n')


class Fake:
    def same(self, outhandle):
        outhandle.write(EXPECTED)

    def differs(self, outhandle):
        outhandle.write(CHANGED)

    def nobaseline(self, outhandle):
        outhandle.write('fresh output')

    def boom(self, outhandle):
        outhandle.write('partial')
        print('about to fail')
        raise ValueError('bad thing happened')

    def truthy(self):
        return True

    def falsy(self):
        return False


class Other:
    def __init__(self):
        self.ready = True

    def only(self, outhandle):
        outhandle.write('other')


class Broken:
    def __init__(self):
        raise RuntimeError('database unreachable')

    def never(self, outhandle):
        pass


def make_regression():
    fake = types.SimpleNamespace()
    for name in ('PASSED', 'FAILED', 'ERROR', 'NO_EXPECTED', 'compare_files',
                 'output_filename', 'get_class_tests'):
        setattr(fake, name, getattr(app_standin, name))
    fake.MODULES = {
        'fake':   (Fake,   lambda storage, srcs: Fake()),
        'other':  (Other,  lambda storage, srcs: Other()),
        'broken': (Broken, lambda storage, srcs: Broken()),
    }
    fake.load_regress_objs = lambda storage, srcs, modules: {
        m: fake.MODULES[m][1](storage, srcs) for m in modules}
    return fake


def write_expected(directory):
    os.makedirs(os.path.join(directory, 'expected'), exist_ok = True)
    for name, text in (('same', EXPECTED), ('differs', EXPECTED), ('only', 'other')):
        with open(os.path.join(directory, 'expected', name), 'w') as handle:
            handle.write(text)


REPO_KEY = 'indianlaw'


def repo_config(root, **overrides):
    values = dict(key = REPO_KEY, name = 'Indian Law', adapter = 'comparison', repo_dir = os.path.join(root, 'clones', 'indianlaw'),
                  options = {'env_profile': 'local'}, python = 'python3', workers = ['w1', 'w2', 'w3'],
                  branches = [repos.BranchConfig('testbranch'), repos.BranchConfig('main')])
    values.update(overrides)
    return repos.RepoConfig(**values)


def write_profiles(results_dir, *configs):
    """The snapshot the runner reads, in the results folder (where it looks by default). Returns its path."""
    path = os.path.join(results_dir, repos.SNAPSHOT_NAME)
    repos.save_snapshot(path, list(configs) or [repo_config(os.path.dirname(results_dir))])
    return path


def run_args(results_dir, **overrides):
    values = dict(tests = '', all = True, base = None, branch = 'testbranch',
                  results_dir = results_dir, repo = REPO_KEY, tree = None,
                  profiles_file = os.path.join(results_dir, repos.SNAPSHOT_NAME),
                  run_id = 'r1', trigger = 'manual',
                  requested_by = 'tester', reason = 'unit test', build_number = '7',
                  build_url = 'http://bb.example/builders/1/builds/7')
    values.update(overrides)
    if not os.path.exists(values['profiles_file']):
        write_profiles(results_dir)
    return argparse.Namespace(**values)


def sharded_args(results_dir, **overrides):
    values = dict(max_shards = 32, worker = 'w1', shard_index = 1)
    values.update(overrides)
    return run_args(results_dir, **values)


@contextlib.contextmanager
def fake_suite(workdir, load_error = None, broken_import = False, fake = None):
    """The indianlaw adapter, with its `regression` module replaced by a fake one and no real clone needed."""
    fake = fake or make_regression()

    def load(self):
        if load_error:
            raise load_error
        return fake, None, []

    def import_regression(self):
        if broken_import:
            raise ImportError('cannot import regression')
        return fake

    saved = (sys.stdout, sys.stderr, os.getcwd())
    patches = [mock.patch.object(OutputComparisonAdapter, 'activate', lambda self: None),
               mock.patch.object(OutputComparisonAdapter, 'load_environment', load),
               mock.patch.object(OutputComparisonAdapter, 'import_regression', import_regression),
               mock.patch.object(GitRepo, 'verify', lambda self: None)]
    for patch in patches:
        patch.start()
    os.chdir(workdir)
    try:
        yield fake
    finally:
        for patch in patches:
            patch.stop()
        sys.stdout, sys.stderr, cwd = saved
        os.chdir(cwd)


def run_with_fake(results_dir, workdir, **overrides):
    with fake_suite(workdir):
        return runs.cmd_run(run_args(results_dir, **overrides))


def call_with_fake(command, results_dir, workdir, load_error = None, broken_import = False, **overrides):
    with fake_suite(workdir, load_error = load_error, broken_import = broken_import):
        return getattr(runs, command)(sharded_args(results_dir, **overrides))
