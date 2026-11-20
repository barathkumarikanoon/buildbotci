import argparse
import os
import sys
import types

import runner
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


def run_args(results_dir, **overrides):
    values = dict(tests = '', all = True, base = None, branch = 'testbranch',
                  results_dir = results_dir, profile = 'local', datasrc = [],
                  loglevel = 'info', run_id = 'r1', trigger = 'manual',
                  requested_by = 'tester', reason = 'unit test', build_number = '7',
                  build_url = 'http://bb.example/builders/1/builds/7', repo_dir = None)
    values.update(overrides)
    return argparse.Namespace(**values)


def run_with_fake(results_dir, workdir, **overrides):
    saved = (sys.stdout, sys.stderr, os.getcwd(), runner.ensure_paths, runner.load_environment)
    fake  = make_regression()
    runner.ensure_paths = lambda repo_dir = None: None
    runner.load_environment = lambda args: (fake, None, [])
    os.chdir(workdir)
    try:
        return runner.cmd_run(run_args(results_dir, **overrides))
    finally:
        sys.stdout, sys.stderr, cwd, runner.ensure_paths, runner.load_environment = saved
        os.chdir(cwd)


def sharded_args(results_dir, **overrides):
    values = dict(max_shards = 32, worker = 'w1', shard_index = 1)
    values.update(overrides)
    return run_args(results_dir, **values)


def call_with_fake(command, results_dir, workdir, load_error = None, broken_import = False, **overrides):
    saved = (sys.stdout, sys.stderr, os.getcwd(), runner.ensure_paths, runner.load_environment)
    fake  = make_regression()
    runner.ensure_paths = lambda repo_dir = None: None

    def load(args):
        if load_error:
            raise load_error
        return fake, None, []

    runner.load_environment = load
    sys.modules['regression'] = None if broken_import else fake
    os.chdir(workdir)
    try:
        return getattr(runner, command)(sharded_args(results_dir, **overrides))
    finally:
        sys.stdout, sys.stderr, cwd, runner.ensure_paths, runner.load_environment = saved
        sys.modules.pop('regression', None)
        os.chdir(cwd)
