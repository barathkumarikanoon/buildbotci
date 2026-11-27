"""Which scripts in bin/ ask for a website superuser, and which do not.

The real scripts run against a stand-in Python (and pip, buildbot, ...) that only writes down how it was called, so
nothing real is started, changed or deleted.
"""
import os
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\nexit 0\n'

# (script and arguments, asks for a login?)
CASES = [
    (['master', 'status'], False), (['master', 'check', '--direct'], False),
    (['master', 'start', '--direct'], True), (['master', 'update', '--direct'], True), (['master', 'stop', '--direct'], True),
    (['master', 'restart', '--direct'], True), (['master', 'reconfig', '--direct'], True),
    (['workers', 'status'], False), (['workers', 'start'], True), (['workers', 'stop', 'indianlaw'], True),
    (['workers', 'restart', '--worker', 'x'], True), (['workers', 'start', '--dry-run'], False),
    (['create-workers'], True), (['create-workers', 'indianlaw'], True),
    (['delete-workers', '--orphans'], False), (['delete-workers', '--orphans', '--yes'], True),
    (['prune-runs', '--days', '30'], False), (['prune-runs', '--days', '30', '--yes'], True),
    (['clean-worktrees', '--dry-run'], False), (['clean-worktrees'], True), (['clean-worktrees', '--all'], True),
    (['clear-queue'], False), (['clear-queue', '--yes'], True),
    (['apply-update'], True),
    (['run-ui'], False),
]


class BinLoginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.venv = os.path.join(self.tmp.name, 'venv')
        os.makedirs(os.path.join(self.venv, 'bin'))
        os.makedirs(os.path.join(self.venv, 'master'))
        for name in ('python', 'pip', 'buildbot', 'buildbot-worker', 'gunicorn'):
            path = os.path.join(self.venv, 'bin', name)
            with open(path, 'w') as handle:
                handle.write(STUB)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.env_file = os.path.join(self.tmp.name, 'settings.env')
        open(self.env_file, 'w').close()
        self.log = os.path.join(self.tmp.name, 'calls.log')
        self.env = dict(os.environ, CI_UI_VENV = self.venv, CI_ENV = self.env_file, STUB_LOG = self.log, HOME = self.tmp.name,
                        CI_UI_PID_FILE = os.path.join(self.tmp.name, 'ui.pid'))

    def calls(self):
        try:
            with open(self.log) as handle:
                return handle.read()
        except OSError:
            return ''

    def test_each_script_asks_exactly_when_it_changes_something(self):
        for args, asks in CASES:
            with self.subTest(command = ' '.join(args)):
                if os.path.exists(self.log):
                    os.remove(self.log)
                done = subprocess.run([os.path.join(ROOT, 'bin', args[0])] + args[1:], env = self.env, cwd = ROOT,
                                      stdout = subprocess.PIPE, stderr = subprocess.PIPE, universal_newlines = True)
                self.assertNotIn('syntax error', done.stderr)
                self.assertEqual('ci_login' in self.calls(), asks, 'calls:\n%s\nstderr:\n%s' % (self.calls(), done.stderr))

    def test_a_refused_login_stops_the_script_before_it_does_anything(self):
        refusing = os.path.join(self.venv, 'bin', 'python')
        with open(refusing, 'w') as handle:
            handle.write('#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\ncase "$*" in *ci_login*) exit 1;; esac\nexit 0\n')
        for args in (['workers', 'stop'], ['create-workers'], ['master', 'update', '--direct'], ['apply-update'],
                     ['delete-workers', '--orphans', '--yes'], ['prune-runs', '--days', '3', '--yes'], ['clear-queue', '--yes'],
                     ['clean-worktrees']):
            with self.subTest(command = ' '.join(args)):
                if os.path.exists(self.log):
                    os.remove(self.log)
                done = subprocess.run([os.path.join(ROOT, 'bin', args[0])] + args[1:], env = self.env, cwd = ROOT,
                                      stdout = subprocess.PIPE, stderr = subprocess.PIPE)
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(self.calls().strip().count('\n'), 0, self.calls())     # only the login attempt ran

    def test_start_installs_master_cfg_when_the_folder_has_none(self):
        target = os.path.join(self.venv, 'master', 'master.cfg')
        self.assertFalse(os.path.exists(target))
        done = subprocess.run([os.path.join(ROOT, 'bin', 'master'), 'start', '--direct'], env = self.env, cwd = ROOT,
                              stdout = subprocess.PIPE, stderr = subprocess.PIPE, universal_newlines = True)
        self.assertTrue(os.path.exists(target), done.stdout + done.stderr)
        self.assertIn('installed master.cfg', done.stdout)
        self.assertIn('checkconfig', self.calls())
        self.assertIn('start', self.calls())

    def test_start_leaves_an_existing_master_cfg_alone(self):
        target = os.path.join(self.venv, 'master', 'master.cfg')
        with open(target, 'w') as handle:
            handle.write('# mine\n')
        subprocess.run([os.path.join(ROOT, 'bin', 'master'), 'start', '--direct'], env = self.env, cwd = ROOT,
                       stdout = subprocess.PIPE, stderr = subprocess.PIPE)
        with open(target) as handle:
            self.assertEqual(handle.read(), '# mine\n')

    def test_the_two_scripts_that_systemd_runs_never_ask(self):
        for name in ('wait-for-db', 'run-ui'):
            with open(os.path.join(ROOT, 'bin', name)) as handle:
                self.assertNotIn('ci_login', handle.read(), name)


if __name__ == '__main__':
    unittest.main()
