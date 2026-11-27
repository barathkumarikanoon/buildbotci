import contextlib
import io
import time
from unittest import mock

from django.test import SimpleTestCase, TestCase

from ciweb.runs import activity, buildbot_client
from ciweb.runs.tests.test_views import ResultsMixin, make_user
from hub.tests import fixtures

OLD = '2020-01-01T00:00:00+0000'
NOTHING = {'running': {}, 'waiting': {}}


class SettledStateTest(SimpleTestCase):
    """A run's record says `running` until the run writes its end; Buildbot says what is true."""

    def run_of(self, **extra):
        return dict({'id': 'r1', 'state': 'running', 'created': OLD}, **extra)

    def test_a_build_that_is_executing_is_running(self):
        self.assertEqual(activity.settled_state(self.run_of(), {'running': {'r1': 2}, 'waiting': {}}), 'running')

    def test_a_request_nobody_took_is_waiting_for_a_worker_even_when_the_record_says_running(self):
        # the build went back with RETRY: Buildbot holds a new request for it
        self.assertEqual(activity.settled_state(self.run_of(), {'running': {}, 'waiting': {'r1': 1}}), 'waiting')

    def test_a_run_with_nothing_in_buildbot_is_stalled(self):
        self.assertEqual(activity.settled_state(self.run_of(), NOTHING), 'stalled')

    def test_a_run_just_made_is_not_judged_before_buildbot_can_know_it(self):
        fresh = time.strftime('%Y-%m-%dT%H:%M:%S+0000', time.gmtime())
        self.assertEqual(activity.settled_state(self.run_of(created = fresh), NOTHING), 'running')

    def test_without_an_answer_from_buildbot_the_record_is_kept(self):
        self.assertEqual(activity.settled_state(self.run_of(), None), 'running')

    def test_a_finished_run_is_never_changed(self):
        self.assertEqual(activity.settled_state(self.run_of(state = 'finished'), NOTHING), 'finished')

    def test_the_summary_counts_each_kind_in_words(self):
        runs = [{'state': 'running'}, {'state': 'running'}, {'state': 'waiting'}, {'state': 'stalled'}]
        found = activity.summary(runs)
        self.assertEqual((found['running'], found['waiting'], found['stalled'], found['busy']), (2, 1, 1, 3))
        self.assertEqual(found['text'], '2 running, 1 waiting for a worker, 1 stalled')
        self.assertEqual(activity.summary([])['text'], '')


class LiveRunsClientTest(SimpleTestCase):
    def test_it_reads_builds_and_unclaimed_requests(self):
        answers = {
            'builds': {'builds': [{'properties': {'run_id': ['r1', 'x']}}, {'properties': {'run_id': ['r1', 'x']}}, {'properties': {}}]},
            'buildrequests': {'buildrequests': [{'buildsetid': 7}]},
            'buildsets/7/properties': {'properties': [{'run_id': ['r2', 'x']}]},
        }
        with mock.patch.object(buildbot_client, '_open', return_value = (None, 'http://bb/', {})), \
                mock.patch.object(buildbot_client, '_get', side_effect = lambda session, base, path, timeout, **params: answers[path]):
            self.assertEqual(buildbot_client.live_runs('http://bb/', 'u', 'p'), {'running': {'r1': 2}, 'waiting': {'r2': 1}})


class PagesAgreeTest(ResultsMixin, TestCase):
    """The sidebar, the overview and the repository page say the same thing about what is in progress."""

    def setUp(self):
        self.build_results()
        with contextlib.redirect_stdout(io.StringIO()):
            fixtures.call_with_fake('cmd_plan', self.results, self.work)             # run r1, recorded as running
        run = self.store.get_run('r1')
        run['created'] = OLD
        self.store.save_run('r1', run)
        self.client.force_login(make_user('alice', is_staff = True))

    def tearDown(self):
        self.drop_results()

    def pages(self, live):
        with mock.patch('ciweb.runs.activity.buildbot_live', return_value = live):
            return [self.client.get(address).content.decode() for address in ('/', '/r/indianlaw/')]

    def test_a_run_buildbot_gave_back_is_waiting_everywhere(self):
        overview, repository = self.pages({'running': {}, 'waiting': {'r1': 1}})
        for html in (overview, repository):
            self.assertIn('waiting for a worker', html)
            self.assertNotIn('class="live-pill">1 running', html)
        self.assertIn('1 waiting', overview)                       # the sidebar
        self.assertNotIn('1 running', overview.split('class="repo-cards"')[1])

    def test_a_running_build_is_running_everywhere(self):
        overview, repository = self.pages({'running': {'r1': 1}, 'waiting': {}})
        self.assertIn('1 running', overview.split('class="repo-cards"')[1])
        self.assertIn('class="live-pill">1 running', overview)
        self.assertIn('1 running', repository)

    def test_a_run_with_nothing_in_buildbot_is_stalled_and_not_counted_as_running(self):
        overview, repository = self.pages(NOTHING)
        self.assertIn('1 stalled', overview)
        self.assertIn('stalled', repository)
        self.assertNotIn('live-pill', overview)

    def test_stopping_a_stalled_run_closes_it(self):
        with mock.patch('ciweb.runs.activity.buildbot_live', return_value = NOTHING), \
                mock.patch.object(buildbot_client, 'stop_run', return_value = {'builds': [], 'requests': [], 'parent_started': False, 'errors': []}):
            self.client.post('/r/indianlaw/run/r1/stop', {'confirm': 'yes'})
        run = self.store.get_run('r1')
        self.assertEqual((run['state'], run['result']), ('aborted', 'aborted'))
