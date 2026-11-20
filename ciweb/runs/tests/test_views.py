import contextlib
import io
import os
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from ciweb.runs import buildbot_client, views
from hub.store import Store
from hub.tests import fixtures

PASSWORD = 'blue-tractor-77x'


def make_user(name, **extra):
    values = dict(is_active = True, approved = True, email_verified = True)
    values.update(extra)
    return get_user_model().objects.create_user(name, '%s@example.com' % name, PASSWORD, **values)


class ResultsMixin:
    @classmethod
    def build_results(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.work = os.path.join(cls.tmp.name, 'work')
        cls.results = os.path.join(cls.tmp.name, 'results')
        os.makedirs(cls.work)
        fixtures.write_expected(cls.work)
        cls.store = Store(cls.results)
        cls.settings = override_settings(RESULTS_DIR = cls.results, BUILDBOT_URL = 'http://bb.example/')
        cls.settings.enable()

    @classmethod
    def drop_results(cls):
        cls.settings.disable()
        cls.tmp.cleanup()


class RunPagesTest(ResultsMixin, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.build_results()
        with contextlib.redirect_stdout(io.StringIO()):
            fixtures.run_with_fake(cls.results, cls.work, trigger = 'push')
        cls.store.save_catalog(cls.store.catalog('testbranch'), 'main')
        cls.store.set_last_tested('testbranch', 'abcdef1234567890', 'r1')

    @classmethod
    def tearDownClass(cls):
        cls.drop_results()
        super().tearDownClass()

    def setUp(self):
        self.client.force_login(make_user('alice'))

    def get(self, path, status = 200):
        reply = self.client.get(path)
        self.assertEqual(reply.status_code, status, path)
        return reply.content.decode()

    def test_runs_list(self):
        html = self.get('/')
        self.assertIn('r1', html)
        self.assertIn('Last scheduled run', html)
        self.assertIn('branch-card failed', html)

    def test_run_page_lists_failures_first_with_reasons(self):
        html = self.get('/run/r1')
        self.assertLess(html.index('fake.boom'), html.index('fake.same'))
        self.assertIn('bad thing happened', html)
        self.assertIn('Re-run 5 failing', html)

    def test_run_page_filters(self):
        html = self.get('/run/r1?status=failing&partial=1')
        self.assertIn('fake.differs', html)
        self.assertNotIn('fake.same', html)
        html = self.get('/run/r1?q=other')
        self.assertIn('other.only', html)
        self.assertNotIn('/run/r1/test/fake.boom', html)

    def test_diff_page_split_view_pinpoints_the_change(self):
        html = self.get('/run/r1/test/fake.differs')
        self.assertIn('Why it failed', html)
        self.assertIn('doc/3', html)
        self.assertIn('doc/18', html)
        self.assertIn('<mark>THREE</mark>', html)
        self.assertIn('<td class="code del">', html)

    def test_diff_page_escapes_output(self):
        html = self.get('/run/r1/test/fake.differs')
        self.assertNotIn('<b>EIGHTEEN', html)
        self.assertIn('&lt;b&gt;', html)

    def test_diff_page_unified_view_and_paging(self):
        html = self.get('/run/r1/test/fake.differs?view=unified&page=1')
        self.assertIn('class="diff unified"', html)
        self.assertIn('class="del"', html)

    def test_error_page_shows_traceback_and_frames(self):
        html = self.get('/run/r1/test/fake.boom')
        self.assertIn('ValueError: bad thing happened', html)
        self.assertIn('Full traceback', html)
        self.assertIn('boom', html)

    def test_a_failed_test_offers_the_complete_files_with_sizes_but_no_server_paths(self):
        html = self.get('/run/r1/test/fake.differs')
        self.assertIn('Full files', html)
        self.assertIn('/run/r1/test/fake.differs/raw/diff?download=1', html)
        self.assertIn('/run/r1/test/fake.differs/raw/expected?download=1', html)
        self.assertIn('/run/r1/test/fake.differs/raw/actual?download=1', html)
        self.assertNotIn(self.results, html)
        self.assertNotIn('tests/fake.differs/diff.txt', html)
        self.assertRegex(html, r'<span class="kind">Full diff</span>\s*<span class="sz">\d+ B</span>')
        reply = self.client.get('/run/r1/test/fake.differs/raw/diff?download=1')
        self.assertEqual(b''.join(reply.streaming_content), open(os.path.join(self.store.test_path('r1', 'fake.differs'), 'diff.txt'), 'rb').read())
        self.assertNotIn('Full files', self.get('/run/r1/test/fake.same'))

    def test_missing_baseline_page_gives_populate_command(self):
        self.assertIn('python regression.py -P local -m fake -t nobaseline -p', self.get('/run/r1/test/fake.nobaseline'))

    def test_passing_page(self):
        self.assertIn('matched the expected file exactly', self.get('/run/r1/test/fake.same'))

    def test_raw_files(self):
        self.assertIn('body THREE', self.get('/run/r1/test/fake.differs/raw/actual'))
        self.assertIn('RELURL: doc/3', self.get('/run/r1/test/fake.differs/raw/expected'))
        self.assertIn('@@', self.get('/run/r1/test/fake.differs/raw/diff'))
        self.assertIn('about to fail', self.get('/run/r1/test/fake.boom/raw/log'))
        download = self.client.get('/run/r1/test/fake.differs/raw/diff?download=1')
        self.assertIn('attachment', download.headers['Content-Disposition'])

    def test_raw_rejects_unknown_kinds_and_missing_files(self):
        self.get('/run/r1/test/fake.same/raw/actual', 404)
        self.get('/run/r1/test/fake.same/raw/result.json', 404)
        self.get('/run/r1/test/fake.same/raw/..', 404)

    def test_path_traversal_ids_are_rejected(self):
        self.get('/run/..%2f..%2fetc/test/x', 404)
        self.get('/run/r1/test/..', 404)
        self.get('/api/run/r1/tail?test=..%2f..', 404)

    def test_unknown_run_and_test(self):
        self.get('/run/nope', 404)
        self.get('/run/r1/test/fake.nope', 404)

    def test_tail_api_is_incremental(self):
        first = self.client.get('/api/run/r1/tail?test=fake.boom').json()
        self.assertIn('about to fail', first['text'])
        again = self.client.get('/api/run/r1/tail?test=fake.boom&offset=%d' % first['offset']).json()
        self.assertEqual(again['text'], '')
        self.assertIn('summary:', self.client.get('/api/run/r1/tail').json()['text'])

    def test_a_bad_page_number_does_not_crash(self):
        self.get('/?page=abc')
        self.get('/run/r1/test/fake.differs?page=abc')

    def test_head_requests_work_for_monitoring(self):
        self.assertEqual(self.client.head('/healthz').status_code, 200)
        self.assertEqual(self.client.head('/').status_code, 200)

    def test_failing_shortcut_also_uses_a_cancelled_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = Store(tmp)
            other.save_catalog(self.store.catalog('main'), 'main')
            other.save_run('r2', dict(self.store.get_run('r1'), id = 'r2', state = 'aborted', result = 'aborted'))
            with override_settings(RESULTS_DIR = tmp):
                self.assertIn('failing in r2', self.get('/new'))

    def test_new_run_form_lists_catalog_and_failing(self):
        html = self.get('/new?tests=fake.same')
        self.assertIn('fake.differs', html)
        self.assertIn('Select 5 failing in r1', html)
        self.assertIn('value="fake.same" checked', html)


class TriggerTest(ResultsMixin, TestCase):
    def setUp(self):
        self.build_results()
        self.store.save_catalog({
            'modules': {'m': {'class': 'M', 'source': 'x.py'}, 'n': {'class': 'N', 'source': 'y.py'}},
            'tests': [{'id': 'm.a', 'module': 'm', 'name': 'a', 'output': 'a', 'has_expected': True},
                      {'id': 'm.b', 'module': 'm', 'name': 'b', 'output': 'b', 'has_expected': True},
                      {'id': 'n.c', 'module': 'n', 'name': 'c', 'output': 'c', 'has_expected': True}]}, 'main')
        self.alice = self.client
        self.alice.force_login(make_user('alice'))
        self.bob = self.client_class()
        self.bob.force_login(make_user('bob'))

    def tearDown(self):
        self.drop_results()

    def submit(self, data, client = None, **patch):
        with mock.patch.object(buildbot_client, 'force_build', **patch) as force:
            reply = (client or self.alice).post('/new', data)
        return reply, force

    def newest(self):
        return self.store.list_runs()[0][0]

    def test_explicit_selection_queues_run_and_calls_buildbot(self):
        reply, force = self.submit({'mode': 'explicit', 'tests': ['m.a', 'm.b', 'n.c'], 'branch': 'main',
                                    'reason': 'check', 'requested_by': 'someone else'}, return_value = [1, {}])
        run = self.newest()
        self.assertEqual(reply.status_code, 302)
        self.assertTrue(reply['Location'].endswith('/run/%s' % run['id']))
        self.assertEqual((run['state'], run['trigger'], run['requested_by']), ('queued', 'manual', 'alice'))
        self.assertEqual([t['id'] for t in run['tests']], ['m.a', 'm.b', 'n.c'])
        params = force.call_args[0][4]
        self.assertEqual(params['tests'], 'm n')
        self.assertEqual((params['branch'], params['run_id']), ('main', run['id']))

    def test_latest_commit_of_the_branch_is_always_requested(self):
        reply, force = self.submit({'mode': 'explicit', 'tests': ['m.a'], 'branch': 'main', 'revision': 'abc1234'}, return_value = None)
        params = force.call_args[0][4]
        self.assertEqual(params['branch'], 'main')
        self.assertNotIn('revision', params)
        self.assertIsNone(self.newest()['revision'])

    def test_every_watched_branch_can_be_requested(self):
        for branch in ('main', '2026'):
            reply, force = self.submit({'mode': 'all', 'branch': branch}, return_value = None)
            self.assertEqual(force.call_args[0][4]['branch'], branch)

    def test_each_branch_has_its_own_test_list(self):
        self.store.save_catalog({'modules': {'z': {'class': 'Z', 'source': 'z.py'}},
                                 'tests': [{'id': 'z.only2026', 'module': 'z', 'name': 'only2026', 'output': 'o', 'has_expected': True}],
                                 'revision': 'abcdef1234567890', 'generated': 'then'}, '2026')
        main_page = self.alice.get('/new?branch=main').content.decode()
        page_2026 = self.alice.get('/new?branch=2026').content.decode()
        self.assertIn('m.a', main_page)
        self.assertNotIn('z.only2026', main_page)
        self.assertIn('z.only2026', page_2026)
        self.assertIn('abcdef12', page_2026)
        reply, force = self.submit({'mode': 'explicit', 'tests': ['m.a'], 'branch': '2026'}, return_value = None)
        force.assert_not_called()
        reply, force = self.submit({'mode': 'explicit', 'tests': ['z.only2026'], 'branch': '2026'}, return_value = None)
        self.assertEqual(force.call_args[0][4]['tests'], 'z')

    def test_a_branch_without_a_test_list_explains_what_to_do_and_hides_the_empty_picker(self):
        html = self.alice.get('/new?branch=2026').content.decode()
        self.assertIn('No test list for <strong>2026</strong> yet', html)
        self.assertIn('Refresh from GitLab', html)
        self.assertIn('Still empty after a minute?', html)
        self.assertNotIn('id="picker"', html)
        self.assertNotIn('0 selected', html)
        self.assertIn('Run everything on 2026', html)
        self.assertIn('/new?branch=main', html)
        self.assertIn('branch-tab', html)
        self.assertIn('no list yet', html)

    def test_the_empty_page_can_still_start_a_run_of_everything(self):
        reply, force = self.submit({'mode': 'all', 'branch': '2026', 'reason': 'no list yet'}, return_value = None)
        self.assertEqual(force.call_args[0][4]['tests'], 'all')
        self.assertEqual(force.call_args[0][4]['branch'], '2026')

    def test_a_failed_refresh_shows_why_and_a_good_refresh_clears_it(self):
        self.store.save_catalog_error('2026', '2026: This branch has no regression.MODULES')
        html = self.alice.get('/new?branch=2026').content.decode()
        self.assertIn('The last refresh failed', html)
        self.assertIn('This branch has no regression.MODULES', html)
        self.store.save_catalog({'modules': {'z': {'class': 'Z', 'source': 'z.py'}}, 'tests': [
            {'id': 'z.t', 'module': 'z', 'name': 't', 'output': 'o', 'has_expected': True}], 'revision': 'abc', 'generated': 'now'}, '2026')
        html = self.alice.get('/new?branch=2026').content.decode()
        self.assertNotIn('The last refresh failed', html)
        self.assertIn('1 module, 1 test,', html)

    def test_counts_use_the_right_plural(self):
        html = self.alice.get('/new?branch=main').content.decode()
        self.assertIn('3 tests', html)
        self.assertNotIn('1 tests', html)

    def test_refresh_asks_buildbot_to_read_the_branch_and_needs_a_login(self):
        with mock.patch.object(buildbot_client, 'force_build', return_value = None) as force:
            reply = self.bob.post('/catalog/refresh', {'branch': '2026'})
            self.assertEqual(reply.status_code, 302)
            self.assertEqual(force.call_args[0][3], 'force-catalog')
            self.assertEqual(force.call_args[0][4], {'branch': '2026', 'reason': 'bob: refresh test list'})
            force.reset_mock()
            reply = self.client_class().post('/catalog/refresh', {'branch': '2026'})
            self.assertIn('/login', reply['Location'])
            self.alice.post('/catalog/refresh', {'branch': 'feature'})
            force.assert_not_called()
            self.assertEqual(self.alice.get('/catalog/refresh').status_code, 405)

    def test_unknown_mode_is_treated_as_explicit_selection(self):
        reply, force = self.submit({'mode': 'impact', 'tests': ['m.a'], 'branch': 'main'}, return_value = None)
        self.assertEqual(force.call_args[0][4]['tests'], 'm.a')

    def test_all_mode(self):
        reply, force = self.submit({'mode': 'all', 'branch': 'main'}, return_value = None)
        self.assertEqual(force.call_args[0][4]['tests'], 'all')
        self.assertEqual(len(self.newest()['tests']), 3)

    def test_buildbot_failure_is_recorded_on_the_run(self):
        reply, force = self.submit({'mode': 'explicit', 'tests': ['m.a'], 'branch': 'main'},
                                   side_effect = buildbot_client.BuildbotError('down'))
        run = self.newest()
        self.assertEqual((run['state'], run['result']), ('finished', 'error'))
        self.assertIn('down', run['message'])
        self.assertIn('Could not queue the run', self.alice.get('/run/%s' % run['id']).content.decode())

    def test_validation(self):
        for data in ({'mode': 'explicit', 'branch': 'main'},
                     {'mode': 'explicit', 'tests': ['m.a'], 'branch': 'bad branch;rm'},
                     {'mode': 'explicit', 'tests': ['m.a'], 'branch': 'feature/xyz'},
                     {'mode': 'explicit', 'tests': ['not.real'], 'branch': 'main'}):
            reply, force = self.submit(data)
            self.assertEqual(reply.status_code, 302)
            force.assert_not_called()
        self.assertEqual(self.store.list_runs()[1], 0)

    def test_runs_are_recorded_under_the_logged_in_developer_not_a_typed_name(self):
        self.submit({'mode': 'all', 'branch': 'main', 'requested_by': 'bob'}, return_value = None)
        self.submit({'mode': 'all', 'branch': 'main'}, client = self.bob, return_value = None)
        self.assertEqual(sorted(r['requested_by'] for r in self.store.list_runs()[0]), ['alice', 'bob'])

    def test_buildbot_reason_starts_with_the_developer_name(self):
        reply, force = self.submit({'mode': 'all', 'branch': 'main', 'reason': 'header fix'}, return_value = None)
        params = force.call_args[0][4]
        self.assertEqual((params['requested_by'], params['reason']), ('alice', 'alice: header fix'))
        reply, force = self.submit({'mode': 'all', 'branch': 'main'}, return_value = None)
        self.assertEqual(force.call_args[0][4]['reason'], 'alice: manual run')

    def test_the_start_page_and_starting_a_run_need_a_login(self):
        stranger = self.client_class()
        reply = stranger.get('/new')
        self.assertEqual(reply.status_code, 302)
        self.assertIn('/login?next=/new', reply['Location'])
        reply, force = self.submit({'mode': 'all', 'branch': 'main'}, client = stranger, return_value = None)
        self.assertIn('/login', reply['Location'])
        force.assert_not_called()
        self.assertEqual(stranger.get('/').status_code, 200)
        self.assertEqual(self.store.list_runs()[1], 0)

    def test_a_form_without_the_right_token_is_refused(self):
        strict = self.client_class(enforce_csrf_checks = True)
        strict.force_login(get_user_model().objects.get(username = 'alice'))
        with mock.patch.object(buildbot_client, 'force_build', return_value = None) as force:
            self.assertEqual(strict.post('/new', {'mode': 'all', 'branch': 'main'}).status_code, 403)
            self.assertEqual(strict.post('/catalog/refresh', {'branch': 'main'}).status_code, 403)
            force.assert_not_called()

    def test_new_page_says_who_the_run_will_be_recorded_as(self):
        html = self.bob.get('/new').content.decode()
        self.assertIn('<strong>bob</strong>', html)
        self.assertNotIn('name="requested_by"', html)


class RequesterFilterTest(ResultsMixin, TestCase):
    def setUp(self):
        self.build_results()
        for run_id, who in (('20260101-000001-a', 'alice'), ('20260101-000002-b', 'Auto'), ('20260101-000003-c', 'bob'),
                            ('20260101-000004-d', 'alice')):
            self.store.save_run(run_id, {'id': run_id, 'state': 'finished', 'result': 'passed', 'requested_by': who,
                                         'trigger': 'push' if who == 'Auto' else 'manual', 'tests': [], 'counts': {'total': 0}})

    def tearDown(self):
        self.drop_results()

    def test_runs_can_be_filtered_by_who_requested_them(self):
        self.assertEqual([r['id'] for r in self.store.list_runs(requested_by = 'alice')[0]],
                         ['20260101-000004-d', '20260101-000001-a'])
        html = self.client.get('/?by=Auto').content.decode()
        self.assertIn('20260101-000002-b', html)
        self.assertNotIn('20260101-000001-a', html)
        page = self.client.get('/').content.decode()
        self.assertLess(page.index('<option value="Auto"'), page.index('<option value="alice"'))


class ShardedRunPageTest(ResultsMixin, TestCase):
    def setUp(self):
        self.build_results()
        with contextlib.redirect_stdout(io.StringIO()):
            fixtures.call_with_fake('cmd_plan', self.results, self.work)
            fixtures.call_with_fake('cmd_shard', self.results, self.work, shard_index = 2, worker = 'w-other')

    def tearDown(self):
        self.drop_results()

    def test_run_page_lists_each_shard_with_its_worker_and_state(self):
        html = self.client.get('/run/r1').content.decode()
        self.assertIn('Workers', html)
        self.assertIn('w-other', html)
        self.assertIn('badge passed', html)
        self.assertIn('badge pending', html)

    def test_live_partial_and_api_reflect_shard_progress(self):
        self.assertIn('other.only', self.client.get('/run/r1?partial=1').content.decode())
        data = self.client.get('/api/run/r1').json()
        self.assertEqual(data['counts']['passed'], 1)
        self.assertEqual(data['counts']['pending'], 7)
        self.assertEqual(data['state'], 'running')


class BuildbotLinkTest(TestCase):
    @override_settings(BUILDBOT_PUBLIC_URL = '/buildbot/')
    def test_the_header_links_to_buildbot_when_it_is_known(self):
        self.assertIn('<a href="/buildbot/" class="navlink buildbot-link">Buildbot ↗</a>', self.client.get('/').content.decode())

    @override_settings(BUILDBOT_PUBLIC_URL = '')
    def test_there_is_no_link_when_nothing_is_configured(self):
        self.assertNotIn('buildbot-link', self.client.get('/').content.decode())


class HelpersTest(TestCase):
    def test_whole_modules_collapse(self):
        catalog = {'tests': [{'id': 'm.a', 'module': 'm'}, {'id': 'm.b', 'module': 'm'}, {'id': 'n.c', 'module': 'n'}, {'id': 'n.d', 'module': 'n'}]}
        self.assertEqual(views.compact_specs(['m.a', 'm.b', 'n.c'], catalog), 'm n.c')
        self.assertEqual(views.compact_specs(['m.a', 'n.c', 'n.d'], catalog), 'n m.a')

    def test_buildbot_client_requires_a_url(self):
        with self.assertRaises(buildbot_client.BuildbotError):
            buildbot_client.force_build('', 'u', 'p', 's', {})
