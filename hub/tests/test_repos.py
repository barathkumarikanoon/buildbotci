import json
import os
import tempfile
import unittest

from hub import repos
from hub.tests import fixtures


def config(**overrides):
    return fixtures.repo_config('/srv', **overrides)


class NamesTest(unittest.TestCase):
    def test_every_part_of_buildbot_is_named_after_the_repo(self):
        c = config(key = 'legal-chatbot')
        self.assertEqual((c.run_builder, c.shard_builder, c.catalog_builder), ('legal-chatbot-run', 'legal-chatbot-shard', 'legal-chatbot-catalog'))
        self.assertEqual((c.force_scheduler_for('main'), c.catalog_scheduler), ('force-legal-chatbot-main', 'catalog-legal-chatbot'))
        self.assertEqual(c.shard_scheduler(3), 'legal-chatbot-shard-3')
        self.assertTrue(c.nightly_scheduler('main').startswith(c.nightly_prefix))

    def test_a_branch_name_becomes_a_name_buildbot_accepts(self):
        c = config(key = 'egazette')
        self.assertEqual(c.force_scheduler_for('release/1.0'), 'force-egazette-release_1_0')
        self.assertEqual(c.nightly_scheduler('release/1.0'), 'egazette-nightly-release_1_0')

    def test_every_branch_has_its_own_time_or_the_repos(self):
        c = config(cron_hour = '21', cron_minute = '0', branches = [
            repos.BranchConfig('main'), repos.BranchConfig('2026', cron_hour = '3', cron_minute = '30'), repos.BranchConfig('dev', cron_hour = '5')])
        self.assertEqual(c.branch_cron('main'), ('21', '0'))
        self.assertEqual(c.branch_cron('2026'), ('3', '30'))
        self.assertEqual(c.branch_cron('dev'), ('5', '0'))

    def test_branches_that_would_get_the_same_scheduler_name_are_refused(self):
        c = config(branches = [repos.BranchConfig('a/b'), repos.BranchConfig('a.b')])
        self.assertTrue(any('twice' in text for field, text in repos.validate(c)))

    def test_a_bad_time_on_a_branch_or_a_too_long_branch_name_is_refused(self):
        bad  = config(branches = [repos.BranchConfig('main', cron_hour = '25')])
        long = config(branches = [repos.BranchConfig('x' * 90)])
        self.assertTrue(any('hour' in text for field, text in repos.validate(bad)))
        self.assertTrue(any('too long' in text for field, text in repos.validate(long)))

    def test_results_of_each_repo_are_in_their_own_folder(self):
        self.assertEqual(config(key = 'egazette').results_dir('/data/results'), '/data/results/egazette')

    def test_the_checkout_is_named_after_the_clone_unless_told_otherwise(self):
        self.assertEqual(config(repo_dir = '/srv/work/egazette', checkout_name = '').checkout_name, 'egazette')
        self.assertEqual(config(repo_dir = '/srv/work/x', checkout_name = 'indianlaw').checkout_name, 'indianlaw')

    def test_only_scheduled_branches_of_a_scheduled_repo_run_by_themselves(self):
        c = config(branches = [repos.BranchConfig('main'), repos.BranchConfig('dev', scheduled = False)])
        self.assertEqual(c.scheduled_branches, ['main'])
        c.schedule = False
        self.assertEqual(c.scheduled_branches, [])


class ValidateTest(unittest.TestCase):
    def fields(self, c, **kw):
        return sorted({name for name, _ in repos.validate(c, **kw)})

    def test_a_good_profile_has_no_problems(self):
        self.assertEqual(repos.validate(config()), [])

    def test_the_key_must_be_a_short_lowercase_name(self):
        for bad in ('Indian', '1abc', 'a', 'has space', 'x' * 40, 'under_score'):
            self.assertEqual(self.fields(config(key = bad)), ['key'], bad)
        for good in ('indianlaw', 'legal-chatbot', 'e2e'):
            self.assertEqual(self.fields(config(key = good)), [], good)

    def test_branches_and_workers_are_required_and_unique(self):
        self.assertIn('branches', self.fields(config(branches = [])))
        self.assertIn('branches', self.fields(config(branches = [repos.BranchConfig('a'), repos.BranchConfig('a')])))
        self.assertIn('branches', self.fields(config(branches = [repos.BranchConfig('bad name')])))
        self.assertEqual(self.fields(config(branches = [repos.BranchConfig('release/1.2')])), [])
        self.assertIn('workers', self.fields(config(workers = [])))
        self.assertIn('workers', self.fields(config(workers = ['a', 'a'])))

    def test_a_worker_serves_one_repo_only(self):
        self.assertIn('workers', self.fields(config(workers = ['w1', 'w9']), taken_workers = {'w1'}))

    def test_cron_fields_use_cron_syntax_in_range(self):
        for hour in ('21', '0,12', '*/6', '*', '9-17'):
            self.assertEqual(self.fields(config(cron_hour = hour)), [], hour)
        for hour in ('24', 'noon', '1;2', ''):
            self.assertEqual(self.fields(config(cron_hour = hour)), ['cron_hour'], hour)
        self.assertEqual(self.fields(config(cron_minute = '60')), ['cron_minute'])

    def test_limits_must_be_positive(self):
        self.assertEqual(self.fields(config(max_runs = 0)), ['max_runs'])
        self.assertEqual(self.fields(config(max_parallel = 0)), [])


class EnvLinesTest(unittest.TestCase):
    def test_lines_become_a_dict(self):
        self.assertEqual(repos.parse_env_lines("# note\nDATADIR=/d\nexport TOKEN='a b'\n\nQ=\"x\"\nEMPTY=\n"),
                         {'DATADIR': '/d', 'TOKEN': 'a b', 'Q': 'x', 'EMPTY': ''})

    def test_a_bad_line_names_its_number(self):
        with self.assertRaises(ValueError) as caught:
            repos.parse_env_lines('A=1\nnot a setting\n')
        self.assertIn('line 2', str(caught.exception))
        with self.assertRaises(ValueError):
            repos.parse_env_lines('1BAD=x')


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'results', 'profiles.json')

    def test_profiles_survive_a_round_trip(self):
        original = config(options = {'env_profile': 'x'}, env = {'A': '1'})
        self.assertTrue(repos.save_snapshot(self.path, [original, config(key = 'egazette', adapter = 'command')]))
        loaded = repos.load_repos(self.path)
        self.assertEqual(sorted(loaded), ['egazette', 'indianlaw'])
        self.assertEqual(loaded['indianlaw'].to_dict(), original.to_dict())
        self.assertEqual([b.name for b in loaded['indianlaw'].branches], ['testbranch', 'main'])

    def test_saving_the_same_profiles_again_changes_nothing(self):
        repos.save_snapshot(self.path, [config()])
        before = os.stat(self.path).st_mtime_ns
        self.assertFalse(repos.save_snapshot(self.path, [config()]))
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)
        self.assertTrue(repos.save_snapshot(self.path, [config(name = 'Renamed')]))

    def test_the_file_is_private_and_never_half_written(self):
        repos.save_snapshot(self.path, [config(env = {'SECRET': 'x'})])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.path)) if n.startswith('.tmp')], [])
        with open(self.path) as handle:
            json.load(handle)

    def test_disabled_profiles_are_kept_but_not_loaded_for_work(self):
        repos.save_snapshot(self.path, [config(enabled = False)])
        self.assertEqual(repos.load_repos(self.path), {})
        self.assertEqual(list(repos.load_repos(self.path, only_enabled = False)), ['indianlaw'])

    def test_no_file_means_nothing_configured_yet(self):
        self.assertEqual(repos.load_repos(os.path.join(self.tmp.name, 'none.json')), {})

    def test_an_unknown_profile_is_explained(self):
        repos.save_snapshot(self.path, [config()])
        with self.assertRaises(SystemExit) as caught:
            repos.get_repo(self.path, 'nope')
        self.assertIn('indianlaw', str(caught.exception))

    def test_the_version_follows_the_content(self):
        a, b = repos.snapshot_text([config()]), repos.snapshot_text([config(name = 'Other')])
        self.assertEqual(repos.text_version(a), repos.text_version(repos.snapshot_text([config()])))
        self.assertNotEqual(repos.text_version(a), repos.text_version(b))

    def test_the_snapshot_is_looked_for_next_to_the_results(self):
        old = os.environ.pop('CI_PROFILES_FILE', None)
        try:
            self.assertEqual(repos.default_snapshot_path('/data/res'), '/data/res/profiles.json')
            os.environ['CI_PROFILES_FILE'] = '/etc/ci/p.json'
            self.assertEqual(repos.default_snapshot_path('/data/res'), '/etc/ci/p.json')
        finally:
            os.environ.pop('CI_PROFILES_FILE', None)
            if old:
                os.environ['CI_PROFILES_FILE'] = old


if __name__ == '__main__':
    unittest.main()


class PythonPathTest(unittest.TestCase):
    """The interpreter of a profile is started without a shell, so $HOME must be expanded for it."""

    def config(self, python):
        return repos.RepoConfig(key = 'x', name = 'X', adapter = 'command', repo_dir = '/srv/x', python = python)

    def test_home_in_the_path_is_expanded(self):
        home = os.path.expanduser('~')
        self.assertEqual(self.config('$HOME/.indianlaw/bin/python').python, home + '/.indianlaw/bin/python')
        self.assertEqual(self.config('~/.indianlaw/bin/python').python, home + '/.indianlaw/bin/python')
        self.assertEqual(self.config('${HOME}/.indianlaw/bin/python').python, home + '/.indianlaw/bin/python')

    def test_an_absolute_path_and_a_bare_name_stay_as_they_are(self):
        self.assertEqual(self.config('/usr/bin/python3').python, '/usr/bin/python3')
        self.assertEqual(self.config('python3').python, 'python3')

    def test_the_command_the_master_sends_to_a_worker_has_no_variable_in_it(self):
        from hub.buildbot import repo_config
        source = repo_config.__file__
        with open(source) as handle:
            self.assertIn('repo.python', handle.read())            # it takes the profile's value, which is expanded
        self.assertNotIn('$', self.config('$HOME/.indianlaw/bin/python').python)

    def test_it_survives_the_snapshot(self):
        import dataclasses
        data = dataclasses.asdict(self.config('$HOME/.indianlaw/bin/python'))
        self.assertEqual(repos.RepoConfig(**data).python, data['python'])
