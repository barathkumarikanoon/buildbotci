import io
import os
from unittest import mock

from django.core.management import call_command
from django.test import override_settings

from ciweb.repos import decommission
from ciweb.repos.models import RepoProfile
from ciweb.repos.tests.test_settings import SettingsTestBase
from ciweb.runs import buildbot_client
from ciweb.runs.tests.test_views import make_repo
from hub import repos as hub_repos
from hub.store import Store, write_json


class DecommissionBase(SettingsTestBase):
    def setUp(self):
        super().setUp()
        self.results = os.path.join(self.tmp.name, 'results')
        self.workers = os.path.join(self.tmp.name, 'workers')
        patch = override_settings(RESULTS_DIR = self.results, BUILDBOT_URL = 'http://bb.example/', BUILDBOT_USER = 'ui', BUILDBOT_PASS = 'p')
        patch.enable()
        self.addCleanup(patch.disable)
        self.profile = make_repo('egazette', worker_names = 'eg-1,eg-2', repo_dir = os.path.join(self.tmp.name, 'egazette'))
        self.config  = self.profile.to_config()
        self.store   = Store(self.config.results_dir(self.results))
        write_json(os.path.join(self.store.runs_dir, 'r1', 'run.json'), {
            'id': 'r1', 'state': 'running', 'tests': [{'id': 'a.x', 'status': 'passed'}, {'id': 'a.y', 'status': 'pending'}]})
        write_json(os.path.join(self.store.runs_dir, 'r0', 'run.json'), {'id': 'r0', 'state': 'finished', 'result': 'passed', 'tests': []})
        for name in ('eg-1', 'eg-2'):
            os.makedirs(os.path.join(self.workers, name))
            open(os.path.join(self.workers, name, 'buildbot.tac'), 'w').write("workername = '%s'\n" % name)
        self.stop = mock.patch.object(buildbot_client, 'stop_builders', return_value = {'requests': 2, 'builds': 1, 'still_running': 0})
        self.stopped_process = mock.patch.object(decommission, 'stop_worker_process', return_value = 'stopped')
        self.stop_mock    = self.stop.start()
        self.process_mock = self.stopped_process.start()
        self.addCleanup(self.stop.stop)
        self.addCleanup(self.stopped_process.stop)

    def delete(self, **post):
        post.setdefault('confirm', 'egazette')
        return self.admin.post('/settings/repos/egazette/delete', post)


class DeletePageTest(DecommissionBase):
    def test_the_page_says_what_will_happen(self):
        html = self.admin.get('/settings/repos/egazette/delete').content.decode()
        self.assertIn('1 run in progress', html)
        self.assertIn('egazette-run, egazette-shard, egazette-catalog', html)
        self.assertIn('eg-1, eg-2', html)
        self.assertIn('stays exactly as it is', html)

    def test_a_warning_dialog_lists_everything_that_will_be_deleted(self):
        html = self.admin.get('/settings/repos/egazette/delete').content.decode()
        self.assertIn('<dialog id="delete-dialog"', html)
        self.assertIn('This cannot be undone', html)
        self.assertIn('1 run in progress</strong> will be stopped', html)
        self.assertIn('egazette-run, egazette-shard, egazette-catalog', html)
        self.assertIn('data-when="workers"', html)
        self.assertIn('data-when="results"', html)
        self.assertIn('data-when="history"', html)
        self.assertIn('repos/repo_delete.js', html)
        self.assertIn('Yes, delete everything listed', html)

    def test_it_changes_nothing_until_the_key_is_typed(self):
        reply = self.delete(confirm = 'nope')
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(RepoProfile.objects.exists())
        self.stop_mock.assert_not_called()
        self.assertTrue(os.path.isdir(os.path.join(self.workers, 'eg-1')))


class DeleteTest(DecommissionBase):
    def test_work_is_stopped_before_the_profile_is_deleted(self):
        order = []
        self.stop_mock.side_effect = lambda *a, **k: (order.append('stop'), RepoProfile.objects.exists() or order.append('PROFILE ALREADY GONE'),
                                                    {'requests': 0, 'builds': 0, 'still_running': 0})[2]
        self.delete()
        self.assertEqual(order, ['stop'])
        self.assertFalse(RepoProfile.objects.exists())
        self.assertEqual(self.snapshot_repos(), {})
        names = self.stop_mock.call_args[0][3]
        self.assertEqual(sorted(names), ['egazette-catalog', 'egazette-run', 'egazette-shard'])

    def test_the_open_run_is_closed_and_finished_runs_are_left_alone(self):
        self.delete()
        run = self.store.get_run('r1')
        self.assertEqual((run['state'], run['result']), ('aborted', 'aborted'))
        self.assertEqual([t['status'] for t in run['tests']], ['passed', 'aborted'])
        self.assertIn('was deleted by admin', run['message'])
        self.assertEqual(self.store.get_run('r0')['result'], 'passed')

    def test_the_workers_on_this_machine_are_stopped_and_their_folders_deleted(self):
        self.delete(workers = '1')
        self.assertEqual(self.process_mock.call_count, 2)
        self.assertFalse(os.path.exists(os.path.join(self.workers, 'eg-1')))
        self.assertFalse(os.path.exists(os.path.join(self.workers, 'eg-2')))

    def test_worker_folders_stay_when_the_box_is_not_ticked(self):
        self.delete()
        self.process_mock.assert_not_called()
        self.assertTrue(os.path.isdir(os.path.join(self.workers, 'eg-1')))

    def test_only_folders_named_like_the_repos_workers_are_touched(self):
        os.makedirs(os.path.join(self.workers, 'other-w1'))
        open(os.path.join(self.workers, 'other-w1', 'buildbot.tac'), 'w').write("workername = 'other-w1'\n")
        self.delete(workers = '1')
        self.assertTrue(os.path.isdir(os.path.join(self.workers, 'other-w1')))

    def test_results_are_kept_unless_asked(self):
        self.delete()
        self.assertTrue(os.path.isdir(self.store.runs_dir))

    def test_results_are_deleted_when_asked_and_only_that_repos_folder(self):
        other = os.path.join(self.results, 'indianlaw', 'runs')
        os.makedirs(other)
        self.delete(results = '1')
        self.assertFalse(os.path.exists(os.path.join(self.results, 'egazette')))
        self.assertTrue(os.path.isdir(other))

    def test_buildbot_history_is_erased_only_when_asked(self):
        target = hub_repos.erase_request_path(self.snapshot)
        self.delete()
        self.assertEqual(hub_repos.read_erase_requests(target), [])
        make_repo('egazette', worker_names = 'eg-1,eg-2')
        self.delete(history = '1')
        self.assertEqual(hub_repos.read_erase_requests(target), ['egazette-catalog', 'egazette-run', 'egazette-shard'])

    def test_adding_the_repo_again_drops_the_old_request_to_erase(self):
        target = hub_repos.erase_request_path(self.snapshot)
        self.delete(history = '1')
        self.assertTrue(hub_repos.read_erase_requests(target))
        from ciweb.repos.tests.test_settings import post_data
        self.admin.post('/settings/repos/new', post_data())
        self.assertEqual(hub_repos.read_erase_requests(target), [])

    def test_an_unreachable_buildbot_does_not_block_the_delete(self):
        self.stop_mock.side_effect = buildbot_client.BuildbotError('Cannot reach Buildbot')
        reply = self.delete()
        self.assertFalse(RepoProfile.objects.exists())
        self.assertEqual(self.store.get_run('r1')['result'], 'aborted')
        page = self.admin.get(reply['Location']).content.decode()
        self.assertIn('Buildbot could not be reached', page)

    def test_disabling_stops_the_runs_but_keeps_everything_else(self):
        self.admin.post('/settings/repos/egazette/toggle')
        self.stop_mock.assert_called_once()
        self.assertEqual(self.store.get_run('r1')['result'], 'aborted')
        self.assertTrue(RepoProfile.objects.exists())
        self.assertTrue(os.path.isdir(os.path.join(self.workers, 'eg-1')))
        self.process_mock.assert_not_called()
        self.stop_mock.reset_mock()
        self.admin.post('/settings/repos/egazette/toggle')
        self.stop_mock.assert_not_called()


class CommandTest(DecommissionBase):
    def run_command(self, *args):
        out = io.StringIO()
        call_command('ci_repos', 'remove', 'egazette', *args, stdout = out)
        return out.getvalue()

    def test_without_yes_it_only_describes(self):
        text = self.run_command()
        self.assertIn('1 run(s) in progress', text)
        self.assertIn('Nothing changed', text)
        self.assertTrue(RepoProfile.objects.exists())
        self.stop_mock.assert_not_called()

    def test_with_yes_it_does_everything_the_page_does(self):
        text = self.run_command('--yes', '--erase-results')
        self.assertIn('Deleted the repository profile', text)
        self.assertFalse(RepoProfile.objects.exists())
        self.assertFalse(os.path.exists(os.path.join(self.results, 'egazette')))
        self.assertFalse(os.path.exists(os.path.join(self.workers, 'eg-1')))

    def test_keep_workers_leaves_their_folders(self):
        self.run_command('--yes', '--keep-workers')
        self.assertTrue(os.path.isdir(os.path.join(self.workers, 'eg-1')))


class RemoveWorkersTest(DecommissionBase):
    """bin/delete-workers: chosen workers only, the repository stays."""

    def setUp(self):
        super().setUp()
        for target, value in (('hub.gitrepo.buildbot_in_use', set()), ('ciweb.repos.decommission.systemd_unit_active', False)):
            patch = mock.patch(target, return_value = value)
            patch.start()
            self.addCleanup(patch.stop)

    def folder(self, name):
        return os.path.join(self.workers, name)

    def run_command(self, *args):
        out = io.StringIO()
        call_command('ci_repos', 'remove-workers', *args, stdout = out)
        return out.getvalue()

    def test_without_yes_nothing_changes(self):
        text = self.run_command('egazette', '--keep-profile')
        self.assertIn('would be stopped', text)
        self.assertIn('Nothing changed', text)
        self.assertTrue(os.path.isdir(self.folder('eg-1')))
        self.process_mock.assert_not_called()

    def test_a_listed_worker_is_refused_without_shrink_or_keep_profile(self):
        text = self.run_command('--worker', 'eg-2', '--yes')
        self.assertIn('still serves repository egazette', text)
        self.assertTrue(os.path.isdir(self.folder('eg-2')))

    def test_shrink_deletes_the_folder_and_takes_the_worker_out_of_the_repository(self):
        self.run_command('egazette', '--worker', 'eg-2', '--shrink', '--yes')
        self.assertFalse(os.path.exists(self.folder('eg-2')))
        self.assertTrue(os.path.isdir(self.folder('eg-1')))
        profile = RepoProfile.objects.get(key = 'egazette')
        self.assertEqual(profile.worker_list(), ['eg-1'])
        self.assertEqual(self.snapshot_repos()['egazette'].workers, ['eg-1'])

    def test_shrink_keeps_default_names_when_the_tail_goes(self):
        make_repo('indianlaw')
        for n in (1, 2, 3):
            os.makedirs(self.folder('indianlaw-w%d' % n))
            open(os.path.join(self.folder('indianlaw-w%d' % n), 'buildbot.tac'), 'w').write("workername = 'x'\n")
        self.run_command('--worker', 'indianlaw-w3', '--shrink', '--yes')
        profile = RepoProfile.objects.get(key = 'indianlaw')
        self.assertEqual((profile.worker_names, profile.worker_count), ('', 2))

    def test_the_last_worker_of_a_repository_is_never_removed_by_shrink(self):
        text = self.run_command('egazette', '--shrink', '--yes')
        self.assertIn('last worker', text)
        self.assertTrue(os.path.isdir(self.folder('eg-2')))
        self.assertEqual(RepoProfile.objects.get(key = 'egazette').worker_list(), ['eg-1', 'eg-2'])

    def test_keep_profile_deletes_only_the_folder(self):
        self.run_command('egazette', '--worker', 'eg-1', '--keep-profile', '--yes')
        self.assertFalse(os.path.exists(self.folder('eg-1')))
        self.assertEqual(RepoProfile.objects.get(key = 'egazette').worker_list(), ['eg-1', 'eg-2'])

    def test_orphans_are_folders_no_repository_lists(self):
        os.makedirs(self.folder('old-w1'))
        open(os.path.join(self.folder('old-w1'), 'buildbot.tac'), 'w').write("workername = 'old-w1'\n")
        self.run_command('--orphans', '--yes')
        self.assertFalse(os.path.exists(self.folder('old-w1')))
        self.assertTrue(os.path.isdir(self.folder('eg-1')))

    def test_a_worker_running_a_build_is_skipped_unless_forced(self):
        with mock.patch('hub.gitrepo.buildbot_in_use', return_value = {('eg-1', 'egazette-shard', None)}):
            text = self.run_command('--worker', 'eg-1', '--keep-profile', '--yes')
            self.assertIn('is running a build', text)
            self.assertTrue(os.path.isdir(self.folder('eg-1')))
            self.run_command('--worker', 'eg-1', '--keep-profile', '--yes', '--force')
            self.assertFalse(os.path.exists(self.folder('eg-1')))

    def test_when_buildbot_cannot_be_asked_nothing_is_deleted_unless_forced(self):
        with mock.patch('hub.gitrepo.buildbot_in_use', return_value = None):
            text = self.run_command('--worker', 'eg-1', '--keep-profile', '--yes')
            self.assertIn('could not be asked', text)
            self.assertTrue(os.path.isdir(self.folder('eg-1')))

    def test_a_worker_that_systemd_runs_is_skipped_even_when_forced(self):
        with mock.patch('ciweb.repos.decommission.systemd_unit_active', return_value = True):
            text = self.run_command('--worker', 'eg-1', '--keep-profile', '--yes', '--force')
            self.assertIn('systemctl disable --now buildbot-worker@eg-1', text)
            self.assertTrue(os.path.isdir(self.folder('eg-1')))

    def test_other_folders_and_unknown_names_are_never_touched(self):
        os.makedirs(os.path.join(self.workers, 'notes'))
        self.run_command('egazette', '--keep-profile', '--yes')
        self.assertTrue(os.path.isdir(os.path.join(self.workers, 'notes')))
        with self.assertRaises(Exception):
            self.run_command('--worker', '../results', '--yes')

    def test_a_worker_of_another_repository_is_refused_under_a_repository_key(self):
        make_repo('indianlaw')
        with self.assertRaises(Exception):
            self.run_command('egazette', '--worker', 'indianlaw-w1')

    def test_it_asks_what_to_delete(self):
        with self.assertRaises(Exception):
            self.run_command()

    def test_a_buildbot_master_folder_is_never_taken_for_a_worker(self):
        os.makedirs(self.folder('master'))
        open(os.path.join(self.folder('master'), 'buildbot.tac'), 'w').write("configfile = 'master.cfg'\n")
        os.makedirs(self.folder('old-w1'))
        open(os.path.join(self.folder('old-w1'), 'buildbot.tac'), 'w').write("workername = 'old-w1'\n")
        self.run_command('--orphans', '--yes')
        self.assertFalse(os.path.exists(self.folder('old-w1')))
        self.assertTrue(os.path.isdir(self.folder('master')))
        self.assertNotIn('master', decommission.installed_workers())


class WorkerControlTest(DecommissionBase):
    """bin/workers: start, stop, restart and status of chosen workers."""

    def setUp(self):
        super().setUp()
        from ciweb.repos import workerctl
        self.workerctl = workerctl
        self.alive = set()
        self.calls = []

        def fake_run(name, action, systemd):
            self.calls.append((name, action, systemd))
            if action in ('start', 'restart'):
                self.alive.add(name)
            else:
                self.alive.discard(name)
            return True, ''

        for target, kwargs in (
                ('ciweb.repos.workerctl.systemd_installed', {'return_value': False}),
                ('ciweb.repos.workerctl.is_running', {'side_effect': lambda n, s: n in self.alive}),
                ('ciweb.repos.workerctl.pid_of', {'side_effect': lambda n: 4242 if n in self.alive else None}),
                ('ciweb.repos.workerctl.run_control', {'side_effect': fake_run}),
                ('ciweb.repos.workerctl.connections', {'return_value': {'eg-1': {'connected': True, 'busy': 0}}}),
                ('hub.gitrepo.buildbot_in_use', {'return_value': set()})):
            patch = mock.patch(target, **kwargs)
            patch.start()
            self.addCleanup(patch.stop)

    def run_command(self, action, *args):
        out = io.StringIO()
        try:
            call_command('ci_repos', action + '-workers', *args, stdout = out)
        finally:
            self.output = out.getvalue()
        return self.output

    def test_start_with_a_key_starts_all_its_workers_once(self):
        self.run_command('start', 'egazette')
        self.assertEqual(self.calls, [('eg-1', 'start', False), ('eg-2', 'start', False)])
        self.calls.clear()
        text = self.run_command('start', 'egazette')
        self.assertIn('already running', text)
        self.assertEqual(self.calls, [])

    def test_one_worker_only(self):
        self.run_command('start', '--worker', 'eg-2')
        self.assertEqual(self.calls, [('eg-2', 'start', False)])

    def test_without_arguments_every_worker_on_this_machine(self):
        os.makedirs(os.path.join(self.workers, 'old-w1'))
        open(os.path.join(self.workers, 'old-w1', 'buildbot.tac'), 'w').write("workername = 'old-w1'\n")
        self.run_command('start')
        self.assertEqual({c[0] for c in self.calls}, {'eg-1', 'eg-2', 'old-w1'})

    def test_stop_and_restart(self):
        self.alive = {'eg-1', 'eg-2'}
        self.run_command('stop', 'egazette')
        self.assertEqual([c[1] for c in self.calls], ['stop', 'stop'])
        self.assertEqual(self.alive, set())
        self.assertIn('already stopped', self.run_command('stop', 'egazette'))
        self.alive = {'eg-1'}
        self.calls.clear()
        self.run_command('restart', '--worker', 'eg-1')
        self.assertEqual(self.calls, [('eg-1', 'restart', False)])

    def test_a_busy_worker_is_not_stopped_unless_forced(self):
        self.alive = {'eg-1', 'eg-2'}
        with mock.patch('hub.gitrepo.buildbot_in_use', return_value = {('eg-1', 'egazette-shard', None)}):
            with self.assertRaises(Exception):
                self.run_command('stop', 'egazette')
            self.assertIn('is running a build', self.output)
            self.assertEqual(self.calls, [('eg-2', 'stop', False)])
            self.run_command('stop', 'egazette', '--force')
            self.assertEqual(self.alive, set())

    def test_when_buildbot_cannot_be_asked_a_running_worker_is_left_alone(self):
        self.alive = {'eg-1'}
        with mock.patch('hub.gitrepo.buildbot_in_use', return_value = None):
            with self.assertRaises(Exception):
                self.run_command('stop', '--worker', 'eg-1')
            self.assertIn('could not be asked', self.output)
            self.assertEqual(self.calls, [])

    def test_a_stopped_worker_needs_no_question_to_buildbot(self):
        with mock.patch('hub.gitrepo.buildbot_in_use', return_value = None):
            self.run_command('stop', 'egazette')
            self.assertIn('already stopped', self.output)

    def test_dry_run_changes_nothing(self):
        text = self.run_command('start', 'egazette', '--dry-run')
        self.assertIn('would start', text)
        self.assertEqual(self.calls, [])

    def test_status_reports_running_stopped_and_busy(self):
        self.alive = {'eg-1'}
        with mock.patch('ciweb.repos.workerctl.connections', return_value = {'eg-1': {'connected': True, 'busy': 1}}):
            text = self.run_command('status', 'egazette')
        self.assertIn('Worker eg-1 (egazette): running', text)
        self.assertIn('Worker eg-2 (egazette): stopped', text)
        self.assertIn('running (pid 4242), connected to the master, running 1 build(s)', text)
        self.assertEqual(self.calls, [])

    def test_status_warns_about_a_running_worker_the_master_does_not_see(self):
        self.alive = {'eg-2'}
        text = self.run_command('status', '--worker', 'eg-2')
        self.assertIn('NOT connected to the master', text)
        self.assertIn('twistd.log', text)

    def test_status_says_when_buildbot_cannot_be_asked(self):
        with mock.patch('ciweb.repos.workerctl.connections', return_value = None):
            self.assertIn('could not be asked whether it is connected', self.run_command('status', 'egazette'))

    def test_a_missing_folder_is_reported_and_fails_the_command(self):
        import shutil
        shutil.rmtree(os.path.join(self.workers, 'eg-2'))
        with self.assertRaises(Exception):
            self.run_command('start', 'egazette')
        self.assertIn('bin/create-workers makes it', self.output)
        self.assertEqual(self.calls, [('eg-1', 'start', False)])

    def test_unknown_names_and_foreign_workers_are_errors(self):
        make_repo('indianlaw')
        for args in (('--worker', 'nobody'), ('egazette', '--worker', 'indianlaw-w1')):
            with self.assertRaises(Exception):
                self.run_command('start', *args)
        self.assertEqual(self.calls, [])

    def test_systemd_is_used_when_installed_and_can_be_forced_either_way(self):
        with mock.patch('ciweb.repos.workerctl.systemd_installed', return_value = True):
            self.run_command('start', '--worker', 'eg-1')
            self.run_command('start', '--worker', 'eg-2', '--direct')
        self.assertEqual(self.calls, [('eg-1', 'start', True), ('eg-2', 'start', False)])
        with self.assertRaises(Exception):
            self.run_command('start', '--systemd', '--direct')

    def test_stopping_a_worker_that_was_never_created_is_not_an_error(self):
        import shutil
        shutil.rmtree(os.path.join(self.workers, 'eg-2'))
        text = self.run_command('stop', '--worker', 'eg-2')
        self.assertIn('nothing to stop', text)
        self.assertEqual(self.calls, [])


class WorkerInfoTest(DecommissionBase):
    """What Buildbot's workers page shows for each worker."""

    def create(self, *args):
        out = io.StringIO()
        with mock.patch('subprocess.run') as run:
            run.return_value = mock.Mock(returncode = 0, stdout = b'')
            with mock.patch.dict(os.environ, {'BUILDBOT_WORKER_PASSWORD': 'x'}):
                call_command('ci_repos', 'workers', 'egazette', '--create', *args, stdout = out)
        return out.getvalue()

    def info(self, name, part):
        with open(os.path.join(self.workers, name, 'info', part)) as handle:
            return handle.read().strip()

    def test_a_new_worker_says_what_it_is_and_where_it_runs(self):
        import shutil
        shutil.rmtree(os.path.join(self.workers, 'eg-2'))
        with mock.patch.dict(os.environ, {'CI_WORKER_ADMIN': ''}):
            self.create()
        self.assertEqual(self.info('eg-2', 'host'), 'eg-2 runs test modules of Indian Law (egazette)')
        self.assertEqual(self.info('eg-2', 'admin'), 'CI admin')
        self.assertNotIn(self.workers, self.info('eg-2', 'host'))

    def test_the_admin_text_can_be_set(self):
        import shutil
        shutil.rmtree(os.path.join(self.workers, 'eg-2'))
        with mock.patch.dict(os.environ, {'CI_WORKER_ADMIN': 'CI team <ci@example.com>'}):
            self.create()
        self.assertEqual(self.info('eg-2', 'admin'), 'CI team <ci@example.com>')

    def test_buildbots_placeholder_texts_are_replaced(self):
        profile_path = os.path.join(self.workers, 'eg-1')
        open(os.path.join(profile_path, 'buildbot.tac'), 'w').write("workername = 'eg-1'\n")
        os.makedirs(os.path.join(profile_path, 'info'))
        open(os.path.join(profile_path, 'info', 'admin'), 'w').write('Your Name Here <admin@youraddress.invalid>\n')
        open(os.path.join(profile_path, 'info', 'host'), 'w').write('Please put a description of this build host here\n')
        with mock.patch.dict(os.environ, {'CI_WORKER_ADMIN': ''}):
            text = self.create()
        self.assertIn('placeholder', text)
        self.assertEqual(self.info('eg-1', 'admin'), 'CI admin')
        self.assertEqual(self.info('eg-1', 'host'), 'eg-1 runs test modules of Indian Law (egazette)')
        self.assertNotIn('Your Name Here', self.info('eg-1', 'admin'))
        self.assertNotIn('Please put', self.info('eg-1', 'host'))

    def test_a_description_somebody_wrote_is_left_alone(self):
        folder = os.path.join(self.workers, 'eg-1')
        os.makedirs(os.path.join(folder, 'info'))
        open(os.path.join(folder, 'info', 'admin'), 'w').write('Asha <asha@example.com>\n')
        open(os.path.join(folder, 'info', 'host'), 'w').write('Rack 4, second shelf\n')
        self.create()
        self.assertEqual(self.info('eg-1', 'admin'), 'Asha <asha@example.com>')
        self.assertEqual(self.info('eg-1', 'host'), 'Rack 4, second shelf')
