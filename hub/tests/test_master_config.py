import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from hub import repos
from hub.tests import fixtures

CI_DIR  = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILDBOT = os.path.join(os.path.dirname(sys.executable), 'buildbot')


@unittest.skipUnless(os.path.exists(BUILDBOT), 'Buildbot is not installed in this Python')
class MasterConfigTest(unittest.TestCase):
    """The real master.cfg, read by Buildbot's own checker: every repo profile becomes valid builders and schedulers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = os.path.join(self.tmp.name, 'results')
        os.makedirs(self.results)
        self.master = os.path.join(self.tmp.name, 'master')
        os.makedirs(self.master)
        shutil.copy(os.path.join(CI_DIR, 'hub', 'buildbot', 'master.cfg'), self.master)

    def environment(self):
        return dict(os.environ, BUILDBOT_DB_URL = 'sqlite:///' + os.path.join(self.tmp.name, 'bb.sqlite'),
                    BUILDBOT_WORKER_PASSWORD = 'w', BUILDBOT_ADMIN_PASSWORD = 'a', BUILDBOT_URL = 'http://localhost:8010/',
                    CI_BUILDBOT_URL = 'http://localhost:8010/', CI_DIR = CI_DIR, CI_RESULTS_DIR = self.results,
                    CI_WORKERS_DIR = os.path.join(self.tmp.name, 'workers'))

    def check(self, *configs):
        repos.save_snapshot(os.path.join(self.results, repos.SNAPSHOT_NAME), list(configs))
        env = dict(os.environ, BUILDBOT_DB_URL = 'sqlite:///' + os.path.join(self.tmp.name, 'bb.sqlite'),
                   BUILDBOT_WORKER_PASSWORD = 'w', BUILDBOT_ADMIN_PASSWORD = 'a', BUILDBOT_URL = 'http://localhost:8010/',
                   CI_BUILDBOT_URL = 'http://localhost:8010/', CI_DIR = CI_DIR, CI_RESULTS_DIR = self.results,
                   CI_WORKERS_DIR = os.path.join(self.tmp.name, 'workers'))
        env.pop('CI_PROFILES_FILE', None)
        done = subprocess.run([BUILDBOT, 'checkconfig', self.master], env = env, cwd = self.tmp.name,
                              stdout = subprocess.PIPE, stderr = subprocess.STDOUT)
        return done.returncode, done.stdout.decode()

    def test_the_ui_account_may_cancel_a_queued_build_request(self):
        """Buildbot sends the cancelling of a queued request as `cancel`; its own stop rule only knows `stop`, which
        left the web UI's account (role forcers) refused with "you need to have role 'admins'"."""
        probe = ("ns = {'__file__': 'master.cfg'}\n"
                 "exec(compile(open('%s').read(), 'master.cfg', 'exec'), ns)\n"
                 "rules = ns['BuildmasterConfig']['www']['authz'].allowRules\n"
                 "stop = [r for r in rules if hasattr(r, 'match_BuildRequestEndpoint_stop')][0]\n"
                 "assert stop.role == 'forcers', stop.role\n"
                 "assert stop.match_BuildRequestEndpoint_cancel.__func__ is stop.match_BuildRequestEndpoint_stop.__func__\n"
                 "print('ok')\n") % os.path.join(self.master, 'master.cfg')
        repos.save_snapshot(os.path.join(self.results, repos.SNAPSHOT_NAME), [])
        done = subprocess.run([sys.executable, '-c', probe], env = self.environment(), cwd = self.tmp.name,
                              stdout = subprocess.PIPE, stderr = subprocess.STDOUT)
        self.assertEqual(done.returncode, 0, done.stdout.decode())

    def test_every_branch_gets_its_own_nightly_and_force_scheduler(self):
        config = fixtures.repo_config(self.tmp.name, key = 'egazette', adapter = 'command', options = {'checks': 'a.b | true'},
                                      workers = ['e1'], cron_hour = '21', cron_minute = '0', max_shards = 2, branches = [
            repos.BranchConfig('main'), repos.BranchConfig('release/1.0', cron_hour = '3', cron_minute = '30'),
            repos.BranchConfig('dev', scheduled = False)])
        repos.save_snapshot(os.path.join(self.results, repos.SNAPSHOT_NAME), [config])
        probe = ("ns = {'__file__': 'master.cfg'}\n"
                 "exec(compile(open('%s').read(), 'master.cfg', 'exec'), ns)\n"
                 "import json\n"
                 "out = {}\n"
                 "for s in ns['BuildmasterConfig']['schedulers']:\n"
                 "    out[s.name] = [s._config_kwargs.get('hour'), s._config_kwargs.get('minute')] if 'nightly' in s.name else s.__class__.__name__\n"
                 "print(json.dumps(out))\n") % os.path.join(self.master, 'master.cfg')
        done = subprocess.run([sys.executable, '-c', probe], env = self.environment(), cwd = self.tmp.name,
                              stdout = subprocess.PIPE, stderr = subprocess.STDOUT)
        self.assertEqual(done.returncode, 0, done.stdout.decode())
        found = __import__('json').loads(done.stdout.decode().strip().splitlines()[-1])
        self.assertEqual(found['egazette-nightly-main'], [21, 0])
        self.assertEqual(found['egazette-nightly-release_1_0'], [3, 30])
        self.assertNotIn('egazette-nightly-dev', found)
        for branch in ('main', 'release_1_0', 'dev'):
            self.assertEqual(found['force-egazette-' + branch], 'ForceScheduler')
        self.assertNotIn('force-egazette', found)

    def test_an_empty_installation_still_starts(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_repos_of_every_suite_type_and_their_own_workers_load_together(self):
        code, out = self.check(
            fixtures.repo_config(self.tmp.name, options = {'env_profile': 'local', 'profile_file': '/x/y.env'}, max_parallel = 2),
            fixtures.repo_config(self.tmp.name, key = 'egazette', adapter = 'command', options = {'checks': 'a.b | true'}, workers = ['eg-1', 'eg-2', 'eg-3'],
                                 cron_hour = '3,15', cron_minute = '*/20', branches = [repos.BranchConfig('master'), repos.BranchConfig('release/1.0', scheduled = False)]),
            fixtures.repo_config(self.tmp.name, key = 'legal-chatbot', adapter = 'pytest', options = {'paths': 'tests'}, workers = ['lc-1'], schedule = False, max_shards = 4))
        self.assertEqual(code, 0, out)

    def test_a_worker_shared_by_two_repos_is_refused(self):
        code, out = self.check(fixtures.repo_config(self.tmp.name, workers = ['w1']),
                               fixtures.repo_config(self.tmp.name, key = 'egazette', adapter = 'command', workers = ['w1']))
        self.assertNotEqual(code, 0)
        self.assertIn('used by two repository profiles', out)


if __name__ == '__main__':
    unittest.main()
