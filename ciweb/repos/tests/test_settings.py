import io
import unittest
import json
import os
import tempfile
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings

from ciweb.repos import publish
from ciweb.repos.models import Branch, RepoProfile
from ciweb.runs.tests.test_views import make_repo, make_user
from hub import repos as hub_repos


def post_data(**overrides):
    data = {
        'key': 'egazette', 'name': 'eGazette', 'description': 'Gazette crawler', 'enabled': 'on', 'adapter': 'command',
        'repo_dir': '/srv/egazette', 'checkout_name': '', 'python': '/srv/venvs/egazette/bin/python', 'env_text': '',
        'schedule': 'on', 'cron_hour': '3', 'cron_minute': '15', 'worker_count': '3', 'worker_names': '',
        'max_runs': '8', 'max_shards': '32', 'max_parallel': '0', 'step_timeout': '7200', 'step_max_time': '21600',
        'opt_command_checks': 'smoke.imports | python -c "import sync"', 'opt_command_cwd': '.', 'opt_command_compare': 'exit',
        'opt_command_golden_dir': 'expected', 'opt_command_expected_exit': '0', 'opt_command_timeout': '600',
        'branch-TOTAL_FORMS': '2', 'branch-INITIAL_FORMS': '0', 'branch-MIN_NUM_FORMS': '0', 'branch-MAX_NUM_FORMS': '1000',
        'branch-0-name': 'master', 'branch-0-scheduled': 'on', 'branch-1-name': 'dev',
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


class SettingsTestBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snapshot = os.path.join(self.tmp.name, 'profiles.json')
        patch = override_settings(PROFILES_FILE = self.snapshot, WORKERS_DIR = os.path.join(self.tmp.name, 'workers'), AUTO_CREATE_WORKERS = False, AUTO_START_WORKERS = False)
        patch.enable()
        self.addCleanup(patch.disable)
        self.admin = self.client_class()
        self.admin.force_login(make_user('admin', is_staff = True))

    def snapshot_repos(self):
        return hub_repos.load_repos(self.snapshot, only_enabled = False)


class AccessTest(SettingsTestBase):
    def test_only_administrators_can_see_or_change_repo_profiles(self):
        make_repo()
        user = self.client_class()
        user.force_login(make_user('plain'))
        for path in ('/settings/repos', '/settings/repos/new', '/settings/repos/indianlaw'):
            self.assertEqual(self.client_class().get(path).status_code, 302, path)
            self.assertEqual(user.get(path).status_code, 403, path)
            self.assertEqual(self.admin.get(path).status_code, 200, path)
        self.assertEqual(user.post('/settings/repos/new', post_data()).status_code, 403)
        self.assertEqual(user.post('/settings/repos/indianlaw/toggle').status_code, 403)
        self.assertEqual(user.post('/settings/repos/indianlaw/delete', {'confirm': 'indianlaw'}).status_code, 403)
        self.assertEqual(RepoProfile.objects.count(), 1)


class CreateTest(SettingsTestBase):
    def create(self, **overrides):
        return self.admin.post('/settings/repos/new', post_data(**overrides))

    def test_a_new_repo_is_saved_with_its_branches_and_default_workers(self):
        reply = self.create()
        self.assertRedirects(reply, '/settings/repos/egazette', fetch_redirect_response = False)
        repo = RepoProfile.objects.get(key = 'egazette')
        self.assertEqual((repo.adapter, repo.python, repo.cron_hour), ('command', '/srv/venvs/egazette/bin/python', '3'))
        self.assertEqual(repo.options['checks'], 'smoke.imports | python -c "import sync"')
        self.assertEqual(repo.options['timeout'], 600)
        self.assertEqual([(b.name, b.scheduled, b.position) for b in repo.branches.all()], [('master', True, 0), ('dev', False, 1)])
        self.assertEqual(repo.worker_list(), ['egazette-w1', 'egazette-w2', 'egazette-w3'])

    def test_saving_writes_the_snapshot_the_master_reads(self):
        self.create()
        config = self.snapshot_repos()['egazette']
        self.assertEqual((config.adapter, config.workers, config.branch_names), ('command', ['egazette-w1', 'egazette-w2', 'egazette-w3'], ['master', 'dev']))
        self.assertEqual(config.scheduled_branches, ['master'])
        self.assertEqual(config.cron_hour, '3')

    def test_each_repo_can_have_a_different_suite_environment_and_workers(self):
        self.create()
        self.admin.post('/settings/repos/new', post_data(
            key = 'legal-chatbot', name = 'Legal chatbot', adapter = 'pytest', repo_dir = '/srv/legal-chatbot', python = '/srv/venvs/indianlaw/bin/python',
            opt_pytest_paths = 'tests', opt_pytest_timeout = '900', env_text = 'DATADIR=/data\n# comment', worker_count = '2'))
        both = self.snapshot_repos()
        self.assertEqual(sorted(both), ['egazette', 'legal-chatbot'])
        chatbot = both['legal-chatbot']
        self.assertEqual((chatbot.adapter, chatbot.options['paths'], chatbot.env, chatbot.workers),
                         ('pytest', 'tests', {'DATADIR': '/data'}, ['legal-chatbot-w1', 'legal-chatbot-w2']))

    def test_only_the_settings_of_the_chosen_suite_type_are_kept(self):
        self.create(opt_indianlaw_env_profile = 'prod')
        self.assertNotIn('env_profile', RepoProfile.objects.get(key = 'egazette').options)

    def test_a_worker_cannot_serve_two_repos(self):
        make_repo('indianlaw', worker_names = 'w1,w2,w3')
        reply = self.create(worker_names = 'w3, egazette-w9')
        self.assertEqual(reply.status_code, 200)
        self.assertContains(reply, 'Worker w3 already serves another repository profile')
        self.assertFalse(RepoProfile.objects.filter(key = 'egazette').exists())

    def test_wrong_values_are_refused_and_marked(self):
        for overrides, text in (({'key': 'Bad Key'}, 'lowercase'), ({'cron_hour': '25'}, 'between 0 and 23'),
                                ({'env_text': 'not a setting'}, 'line 1'), ({'worker_count': '0'}, 'between 1 and 40'),
                                ({'opt_command_checks': ''}, 'required'), ({'opt_command_cwd': '../x'}, 'inside the checkout'),
                                ({'opt_command_timeout': 'x'}, 'whole number'), ({'branch-0-name': '', 'branch-1-name': '', 'branch-1-scheduled': 'on'}, 'at least one branch'),
                                ({'branch-1-name': 'master', 'branch-1-scheduled': 'on'}, 'duplicate data')):
            reply = self.create(**overrides)
            self.assertEqual(reply.status_code, 200, overrides)
            self.assertContains(reply, text)
            self.assertFalse(RepoProfile.objects.exists(), overrides)

    def test_the_key_is_unique(self):
        self.create()
        self.assertContains(self.create(), 'already exists')

    def test_a_suite_type_with_its_own_checks_validates_them(self):
        self.assertContains(self.create(opt_command_checks = 'no separator'), 'line 1')


class EditTest(SettingsTestBase):
    def setUp(self):
        super().setUp()
        self.admin.post('/settings/repos/new', post_data())

    def test_the_form_shows_what_was_saved(self):
        html = self.admin.get('/settings/repos/egazette').content.decode()
        self.assertIn('value="master"', html)
        self.assertIn('smoke.imports | python -c', html)
        self.assertIn('data-adapter="comparison"', html)
        self.assertIn('data-adapter="pytest"', html)

    def test_editing_changes_the_snapshot_and_a_branch_can_be_added_or_removed(self):
        branch = Branch.objects.get(name = 'dev')
        reply = self.admin.post('/settings/repos/egazette', post_data(**{
            'name': 'eGazette crawler', 'branch-TOTAL_FORMS': '3', 'branch-INITIAL_FORMS': '2',
            'branch-0-id': Branch.objects.get(name = 'master').pk, 'branch-0-repo': RepoProfile.objects.get().pk,
            'branch-1-id': branch.pk, 'branch-1-repo': RepoProfile.objects.get().pk, 'branch-1-DELETE': 'on',
            'branch-2-name': 'release/2', 'branch-2-scheduled': 'on'}))
        self.assertEqual(reply.status_code, 302, reply.content.decode()[:300] if reply.status_code == 200 else '')
        config = self.snapshot_repos()['egazette']
        self.assertEqual((config.name, config.branch_names), ('eGazette crawler', ['master', 'release/2']))

    def test_the_key_cannot_be_changed(self):
        self.admin.post('/settings/repos/egazette', post_data(key = 'renamed', **{
            'branch-INITIAL_FORMS': '0'}))
        self.assertTrue(RepoProfile.objects.filter(key = 'egazette').exists())
        self.assertFalse(RepoProfile.objects.filter(key = 'renamed').exists())

    def test_changing_the_suite_type_replaces_its_settings(self):
        self.admin.post('/settings/repos/egazette', post_data(adapter = 'pytest', opt_pytest_paths = 'tests', **{
            'branch-0-id': Branch.objects.get(name = 'master').pk, 'branch-0-repo': RepoProfile.objects.get().pk,
            'branch-1-id': Branch.objects.get(name = 'dev').pk, 'branch-1-repo': RepoProfile.objects.get().pk, 'branch-INITIAL_FORMS': '2'}))
        self.assertEqual(self.snapshot_repos()['egazette'].adapter, 'pytest')
        self.assertEqual(RepoProfile.objects.get().options['paths'], 'tests')

    def test_disable_and_enable_keep_the_profile_but_tell_the_master(self):
        self.admin.post('/settings/repos/egazette/toggle')
        self.assertFalse(self.snapshot_repos()['egazette'].enabled)
        self.assertEqual(hub_repos.load_repos(self.snapshot), {})
        self.admin.post('/settings/repos/egazette/toggle')
        self.assertEqual(list(hub_repos.load_repos(self.snapshot)), ['egazette'])

    def test_delete_needs_the_key_typed_and_leaves_the_results_on_disk(self):
        results = os.path.join(self.tmp.name, 'results', 'egazette', 'runs', 'r1')
        os.makedirs(results)
        self.admin.post('/settings/repos/egazette/delete', {'confirm': 'nope'})
        self.assertTrue(RepoProfile.objects.exists())
        self.admin.post('/settings/repos/egazette/delete', {'confirm': 'egazette'})
        self.assertFalse(RepoProfile.objects.exists())
        self.assertEqual(self.snapshot_repos(), {})
        self.assertTrue(os.path.isdir(results))

    def test_the_page_says_which_workers_still_have_to_be_created(self):
        html = self.admin.get('/settings/repos/egazette').content.decode()
        self.assertIn('bin/create-workers egazette', html)
        self.assertIn('bin/workers start egazette', html)


class SyncStateTest(SettingsTestBase):
    def test_the_page_follows_what_the_master_reports(self):
        make_repo()
        self.assertEqual(publish.sync_state()[0], 'unknown')
        publish.publish()
        version = hub_repos.text_version(hub_repos.read_snapshot_text(self.snapshot))
        self.assertEqual(publish.sync_state()[0], 'unknown')
        with open(hub_repos.applied_path(self.snapshot), 'w') as handle:
            json.dump({'version': 'old', 'error': None}, handle)
        self.assertEqual(publish.sync_state()[0], 'pending')
        with open(hub_repos.applied_path(self.snapshot), 'w') as handle:
            json.dump({'version': 'old', 'error': 'could not load'}, handle)
        self.assertEqual(publish.sync_state(), ('error', 'could not load'))
        with open(hub_repos.applied_path(self.snapshot), 'w') as handle:
            json.dump({'version': version, 'error': None}, handle)
        self.assertEqual(publish.sync_state()[0], 'applied')
        self.assertContains(self.admin.get('/settings/repos'), 'data-sync="applied"')


class CommandTest(SettingsTestBase):
    def run_command(self, *args, env = None):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env or {}):
            call_command('ci_repos', *args, stdout = out)
        return out.getvalue()

    def test_export_and_list(self):
        make_repo()
        os.remove(self.snapshot) if os.path.exists(self.snapshot) else None
        self.assertIn('written', self.run_command('export'))
        self.assertIn('already up to date', self.run_command('export'))
        self.assertIn('indianlaw', self.run_command('list'))

    def test_workers_are_reported_missing_until_created(self):
        make_repo(worker_names = 'x1,x2')
        self.assertIn('missing  x1', self.run_command('workers'))
        os.makedirs(os.path.join(self.tmp.name, 'workers', 'x1'))
        open(os.path.join(self.tmp.name, 'workers', 'x1', 'buildbot.tac'), 'w').close()
        text = self.run_command('workers')
        self.assertIn('exists   x1', text)
        self.assertIn('missing  x2', text)
        self.assertIn('bin/create-workers', text)

    def test_the_first_profile_is_made_from_the_settings_of_before(self):
        results = os.path.join(self.tmp.name, 'results')
        os.makedirs(os.path.join(results, 'runs', 'r1'))
        os.makedirs(os.path.join(results, 'state'))
        env = {'CI_BRANCHES': 'main,2026', 'CI_PYTHON': '/py/indianlaw', 'CI_WORKERS': 'w1,w2,w3', 'CI_REGRESSION_PROFILE': 'prod',
               'CI_PROFILE_FILE': '/p/private.env', 'CI_REPO_DIR': '/srv/indianlaw', 'CI_CRON_HOUR': '22', 'CI_MAX_PARALLEL_SHARDS': '3'}
        with override_settings(RESULTS_DIR = results):
            self.run_command('seed-legacy', '--dry-run', env = env)
            self.assertFalse(RepoProfile.objects.exists())
            self.run_command('seed-legacy', env = env)
            repo = RepoProfile.objects.get(key = 'indianlaw')
            self.assertEqual((repo.adapter, repo.python, repo.worker_list(), repo.cron_hour, repo.max_parallel),
                             ('comparison', '/py/indianlaw', ['w1', 'w2', 'w3'], '22', 3))
            self.assertEqual(repo.options, {'env_profile': 'prod', 'profile_file': '/p/private.env'})
            self.assertEqual(repo.branch_names, ['main', '2026'])
            self.assertTrue(os.path.isdir(os.path.join(results, 'indianlaw', 'runs', 'r1')))
            self.assertFalse(os.path.exists(os.path.join(results, 'runs')))
            with self.assertRaises(Exception):
                self.run_command('seed-legacy', env = env)


class WorkerStatusTest(SettingsTestBase):
    """The repository pages say which workers are connected to the master."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        self.profile = make_repo('egazette', worker_names = 'eg-1,eg-2,eg-3')
        os.makedirs(os.path.join(self.tmp.name, 'workers', 'eg-2'))
        open(os.path.join(self.tmp.name, 'workers', 'eg-2', 'buildbot.tac'), 'w').close()
        self.states = {'eg-1': {'connected': True, 'busy': 0}, 'eg-2': {'connected': False, 'busy': 0}}
        patch = mock.patch('ciweb.repos.views.worker_states', side_effect = lambda: self.states)
        patch.start()
        self.addCleanup(patch.stop)

    def rows(self):
        from ciweb.repos import health
        return {r['name']: r['state'] for r in health.worker_rows(self.profile.to_config(), os.path.join(self.tmp.name, 'workers'), self.states)}

    def test_each_worker_is_connected_stopped_or_not_created(self):
        self.assertEqual(self.rows(), {'eg-1': 'connected', 'eg-2': 'stopped', 'eg-3': 'not_created'})

    def test_a_worker_running_a_build_is_busy(self):
        self.states['eg-1']['busy'] = 2
        self.assertEqual(self.rows()['eg-1'], 'busy')

    def test_a_connected_worker_on_another_machine_needs_no_folder_here(self):
        self.states['eg-3'] = {'connected': True, 'busy': 0}
        self.assertEqual(self.rows()['eg-3'], 'connected')

    def test_when_buildbot_cannot_be_asked_every_worker_is_unknown(self):
        from ciweb.repos import health
        rows = health.worker_rows(self.profile.to_config(), os.path.join(self.tmp.name, 'workers'), None)
        self.assertEqual({r['state'] for r in rows}, {'unknown'})

    def test_the_repository_page_lists_them_with_what_to_do(self):
        html = self.admin.get('/settings/repos/egazette').content.decode()
        self.assertIn('1 of 3 connected', html)
        self.assertIn('bin/workers start --worker eg-2', html)
        self.assertIn('bin/create-workers egazette', html)

    def test_the_list_page_shows_the_count(self):
        html = self.admin.get('/settings/repos').content.decode()
        self.assertIn('1 of 3 connected', html)
        self.assertIn('worker-connected', html)
        self.assertIn('worker-stopped', html)


class AutoCreateWorkersTest(SettingsTestBase):
    """Saving a repository makes its worker folders on this machine."""

    def setUp(self):
        super().setUp()
        patch = override_settings(AUTO_CREATE_WORKERS = True, AUTO_START_WORKERS = True)
        patch.enable()
        self.addCleanup(patch.disable)
        self.created = []
        self.running = set()
        self.start_calls = []

        def fake_control(name, action, systemd):
            self.start_calls.append((name, action, systemd))
            self.running.add(name)
            return True, ''

        for target, kwargs in (('ciweb.repos.workerctl.run_control', {'side_effect': fake_control}),
                               ('ciweb.repos.workerctl.is_running', {'side_effect': lambda n, s: n in self.running}),
                               ('ciweb.repos.workerctl.use_systemd', {'return_value': False})):
            started = mock.patch(target, **kwargs)
            started.start()
            self.addCleanup(started.stop)

        def fake_run(command, **kwargs):
            folder, name = command[4], command[6]
            os.makedirs(folder, exist_ok = True)
            open(os.path.join(folder, 'buildbot.tac'), 'w').write("workername = '%s'\n" % name)
            self.created.append(name)
            return mock.Mock(returncode = 0, stdout = b'')

        run = mock.patch('ciweb.repos.workerctl.subprocess.run', side_effect = fake_run)
        run.start()
        self.addCleanup(run.stop)
        env = mock.patch.dict(os.environ, {'BUILDBOT_WORKER_PASSWORD': 'pw'})
        env.start()
        self.addCleanup(env.stop)

    def edit_post(self, **overrides):
        """An edit of the saved repository: its two branches sent back with their identifiers."""
        pk = RepoProfile.objects.get(key = 'egazette').pk
        data = {'branch-INITIAL_FORMS': '2', 'branch-0-id': Branch.objects.get(name = 'master').pk, 'branch-0-repo': pk,
                'branch-1-id': Branch.objects.get(name = 'dev').pk, 'branch-1-repo': pk}
        data.update(overrides)
        return self.admin.post('/settings/repos/egazette', post_data(**data))

    def follow(self, response):
        return self.admin.get(response['Location']).content.decode()

    def test_a_new_repository_gets_its_workers(self):
        page = self.follow(self.admin.post('/settings/repos/new', post_data()))
        self.assertEqual(sorted(self.created), ['egazette-w1', 'egazette-w2', 'egazette-w3'])
        self.assertIn('Created the worker folders egazette-w1, egazette-w2, egazette-w3', page)
        self.assertIn('Started the workers egazette-w1, egazette-w2, egazette-w3', page)
        self.assertEqual(sorted(c[0] for c in self.start_calls), ['egazette-w1', 'egazette-w2', 'egazette-w3'])
        with open(os.path.join(self.tmp.name, 'workers', 'egazette-w1', 'info', 'host')) as handle:
            self.assertEqual(handle.read().strip(), 'egazette-w1 runs test modules of eGazette (egazette)')

    def test_saving_again_does_not_create_them_twice(self):
        self.admin.post('/settings/repos/new', post_data())
        self.created.clear()
        self.edit_post()
        self.assertEqual(self.created, [])

    def test_raising_the_number_of_workers_creates_only_the_new_ones(self):
        self.admin.post('/settings/repos/new', post_data())
        self.created.clear()
        self.edit_post(worker_count = '4')
        self.assertEqual(self.created, ['egazette-w4'])

    def test_a_disabled_repository_gets_no_workers(self):
        self.admin.post('/settings/repos/new', post_data(enabled = None))
        self.assertEqual(self.created, [])

    def test_a_problem_warns_but_the_repository_is_saved(self):
        with mock.patch.dict(os.environ, {'BUILDBOT_WORKER_PASSWORD': ''}):
            page = self.follow(self.admin.post('/settings/repos/new', post_data()))
        self.assertTrue(RepoProfile.objects.filter(key = 'egazette').exists())
        self.assertIn('could not all be created', page)
        self.assertIn('BUILDBOT_WORKER_PASSWORD is not set', page)
        self.assertIn('bin/create-workers egazette', page)

    def test_it_can_be_switched_off(self):
        with override_settings(AUTO_CREATE_WORKERS = False):
            self.admin.post('/settings/repos/new', post_data())
        self.assertEqual(self.created, [])

    def test_a_worker_that_is_already_running_is_not_started_again(self):
        self.admin.post('/settings/repos/new', post_data())
        self.start_calls.clear()
        self.edit_post()
        self.assertEqual(self.start_calls, [])

    def test_a_start_problem_warns_but_the_repository_is_saved(self):
        with mock.patch('ciweb.repos.workerctl.run_control', return_value = (False, 'port in use')):
            page = self.follow(self.admin.post('/settings/repos/new', post_data()))
        self.assertTrue(RepoProfile.objects.filter(key = 'egazette').exists())
        self.assertIn('Could not start egazette-w1: port in use', page)

    def test_starting_alone_can_be_switched_off(self):
        with override_settings(AUTO_START_WORKERS = False):
            page = self.follow(self.admin.post('/settings/repos/new', post_data()))
        self.assertIn('Created the worker folders', page)
        self.assertEqual(self.start_calls, [])


class WorkerButtonTest(SettingsTestBase):
    """The button on the Workers card, for a repository that already exists."""

    def setUp(self):
        super().setUp()
        self.profile = make_repo('egazette', worker_names = 'eg-1,eg-2')
        self.running = set()
        self.calls = []
        self.states = {}

        def fake_control(name, action, systemd):
            self.calls.append((name, action))
            self.running.add(name)
            return True, ''

        def fake_create(profile, name):
            folder = os.path.join(self.tmp.name, 'workers', name)
            os.makedirs(folder, exist_ok = True)
            open(os.path.join(folder, 'buildbot.tac'), 'w').write("workername = '%s'\n" % name)

        for target, kwargs in (('ciweb.repos.workerctl.run_control', {'side_effect': fake_control}),
                               ('ciweb.repos.workerctl.create_worker', {'side_effect': fake_create}),
                               ('ciweb.repos.workerctl.is_running', {'side_effect': lambda n, s: n in self.running}),
                               ('ciweb.repos.workerctl.use_systemd', {'return_value': False}),
                               ('ciweb.repos.views.worker_states', {'side_effect': lambda: self.states})):
            patch = mock.patch(target, **kwargs)
            patch.start()
            self.addCleanup(patch.stop)

    def press(self, client = None):
        return (client or self.admin).post('/settings/repos/egazette/workers')

    def test_it_creates_and_starts_even_when_the_automatic_switches_are_off(self):
        page = self.admin.get(self.press()['Location']).content.decode()
        self.assertIn('Created the worker folders eg-1, eg-2', page)
        self.assertIn('Started the workers eg-1, eg-2', page)
        self.assertEqual(sorted(self.calls), [('eg-1', 'start'), ('eg-2', 'start')])

    def test_pressing_it_twice_does_nothing_the_second_time(self):
        self.press()
        self.calls.clear()
        page = self.admin.get(self.press()['Location']).content.decode()
        self.assertEqual(self.calls, [])
        self.assertIn('Nothing to do', page)

    def test_the_card_shows_the_button_only_when_something_needs_it(self):
        html = self.admin.get('/settings/repos/egazette').content.decode()
        self.assertIn('Create and start workers', html)
        self.states = {'eg-1': {'connected': True, 'busy': 0}, 'eg-2': {'connected': True, 'busy': 0}}
        html = self.admin.get('/settings/repos/egazette').content.decode()
        self.assertNotIn('Create and start workers', html)

    def test_only_administrators_may_press_it(self):
        user = self.client_class()
        user.force_login(make_user('plain'))
        self.assertEqual(self.press(user).status_code, 403)
        self.assertEqual(self.press(self.client_class()).status_code, 302)
        self.assertEqual(self.calls, [])

    def test_a_disabled_repository_is_refused(self):
        self.profile.enabled = False
        self.profile.save()
        page = self.admin.get(self.press()['Location']).content.decode()
        self.assertIn('is disabled', page)
        self.assertEqual(self.calls, [])

    def test_get_is_not_allowed(self):
        self.assertEqual(self.admin.get('/settings/repos/egazette/workers').status_code, 405)


class CleanEnvironmentTest(unittest.TestCase):
    """A worker started by this program gets none of the CI's settings."""

    def test_secrets_are_left_out_and_the_basics_stay(self):
        from ciweb.repos import workerctl
        env = {'PATH': '/usr/bin', 'HOME': '/home/x', 'LANG': 'C.UTF-8', 'LC_ALL': 'C', 'CI_UI_SECRET': 's', 'BUILDBOT_DB_URL': 'postgresql://u:p@h/d',
               'BUILDBOT_WORKER_PASSWORD': 'w', 'CI_ADMIN_PASSWORD': 'a', 'AWS_SECRET_ACCESS_KEY': 'k'}
        with mock.patch.dict(os.environ, env, clear = True):
            cleaned = workerctl.clean_environment()
        self.assertEqual(set(cleaned), {'PATH', 'HOME', 'LANG', 'LC_ALL'})

    def test_a_direct_start_uses_it_and_systemd_does_not(self):
        from ciweb.repos import workerctl
        with mock.patch('ciweb.repos.workerctl.subprocess.run', return_value = mock.Mock(returncode = 0, stdout = b'')) as run, \
             mock.patch('os.path.exists', return_value = True), mock.patch.dict(os.environ, {'CI_UI_SECRET': 's'}):
            workerctl.run_control('eg-1', 'start', False)
            self.assertNotIn('CI_UI_SECRET', run.call_args.kwargs['env'])
            workerctl.run_control('eg-1', 'start', True)
            self.assertIsNone(run.call_args.kwargs['env'])
