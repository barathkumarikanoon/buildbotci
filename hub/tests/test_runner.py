import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import runner
from hub.store import Store
from hub.tests import fixtures


class AssignShardsTest(unittest.TestCase):
    def test_one_module_per_shard(self):
        shards = runner.assign_shards(['a', 'b', 'c'], 32)
        self.assertEqual([s['modules'] for s in shards], [['a'], ['b'], ['c']])
        self.assertEqual([s['index'] for s in shards], [1, 2, 3])

    def test_modules_are_grouped_beyond_the_limit(self):
        shards = runner.assign_shards(['a', 'b', 'c', 'd', 'e'], 2)
        self.assertEqual([s['modules'] for s in shards], [['a', 'c', 'e'], ['b', 'd']])

    def test_nothing_selected_gives_no_shards(self):
        self.assertEqual(runner.assign_shards([], 32), [])


class ShardedRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = os.path.join(self.tmp.name, 'work')
        self.results = os.path.join(self.tmp.name, 'results')
        os.makedirs(self.work)
        fixtures.write_expected(self.work)
        self.store = Store(self.results)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, command, **overrides):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = fixtures.call_with_fake(command, self.results, self.work, **overrides)
        return code, out.getvalue()

    def plan(self, **overrides):
        code, out = self.call('cmd_plan', **overrides)
        self.assertEqual(code, 0, out)
        return out

    def test_plan_prints_ids_for_buildbot_and_creates_one_shard_per_module(self):
        out = self.plan()
        self.assertIn('RUN_ID: r1', out)
        self.assertIn('SHARDS: 3', out)
        run = self.store.get_run('r1')
        self.assertEqual(run['state'], 'running')
        self.assertEqual([s['modules'] for s in run['shards']], [['fake'], ['other'], ['broken']])
        self.assertEqual(run['counts']['pending'], run['counts']['total'])
        self.assertEqual(run['counts']['total'], 8)

    def test_plan_with_explicit_module_makes_a_single_shard(self):
        out = self.plan(all = False, tests = 'other')
        self.assertIn('SHARDS: 1', out)
        self.assertEqual([t['id'] for t in self.store.get_run('r1')['tests']], ['other.only'])

    def test_plan_labels_an_all_request_as_all_and_other_requests_as_explicit(self):
        self.plan(all = False, tests = 'all')
        self.assertEqual(self.store.get_run('r1')['selection']['mode'], 'all')
        self.assertEqual(len(self.store.get_run('r1')['tests']), 8)
        self.plan(all = False, tests = 'other', run_id = 'r2')
        self.assertEqual(self.store.get_run('r2')['selection']['mode'], 'explicit')

    def test_a_branch_without_the_regression_hook_is_explained_in_plain_words(self):
        import types
        with self.assertRaises(RuntimeError) as caught:
            runner.build_catalog(types.SimpleNamespace())
        self.assertIn('regression.MODULES', str(caught.exception))
        self.assertIn('not been merged', str(caught.exception))

    def shard_log(self, **env):
        with mock.patch.dict(os.environ, env):
            self.call('cmd_shard', shard_index = 1)
        with open(os.path.join(self.store.run_path('r1'), 'runner.log'), encoding = 'utf8') as handle:
            return [line.replace('[shard 1] ', '', 1) for line in handle.read().splitlines() if line.startswith('[shard 1] ')]

    def block_of(self, lines, test_id):
        start = next(i for i, line in enumerate(lines) if line.startswith('[') and line.endswith('] ' + test_id))
        end   = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('[')), len(lines))
        return '\n'.join(lines[start:end])

    def test_a_failed_test_says_where_its_complete_files_are_in_the_build_log(self):
        self.plan()
        block = self.block_of(self.shard_log(REG_UI_URL = 'http://ui.example/'), 'fake.differs')
        self.assertRegex(block, r'full diff:\s+\S+/tests/fake\.differs/diff\.txt\s+\(\d+ B\)')
        self.assertRegex(block, r'expected:\s+\S+/tests/fake\.differs/expected\s+\(\d+ B\)')
        self.assertRegex(block, r'actual:\s+\S+/tests/fake\.differs/actual\s+\(\d+ B\)')
        self.assertIn('open it:  http://ui.example/run/r1/test/fake.differs', block)
        self.assertIn('download the full diff:  http://ui.example/run/r1/test/fake.differs/raw/diff?download=1', block)

    def test_without_a_web_address_only_the_paths_are_printed(self):
        self.plan()
        block = self.block_of(self.shard_log(REG_UI_URL = ''), 'fake.differs')
        self.assertIn('full diff:', block)
        self.assertNotIn('open it:', block)

    def test_a_passing_test_prints_no_file_lines(self):
        self.plan()
        lines = self.shard_log()
        self.assertNotIn('full diff:', self.block_of(lines, 'fake.same'))
        self.assertNotIn('full diff:', self.block_of(lines, 'fake.truthy'))

    def test_sizes_are_written_in_plain_units(self):
        self.assertEqual([runner.human_size(n) for n in (0, 999, 1024, 5 * 1024 * 1024, 17731988)], ['0 B', '999 B', '1.0 KB', '5.0 MB', '16.9 MB'])

    def test_plan_rejects_unknown_tests(self):
        code, out = self.call('cmd_plan', all = False, tests = 'nosuch')
        self.assertEqual(code, 2)
        run = self.store.get_run('r1')
        self.assertEqual((run['state'], run['result']), ('finished', 'error'))
        self.assertIn('nosuch', run['message'])

    def test_each_shard_runs_only_its_own_modules_and_reports_separately(self):
        self.plan()
        code, _ = self.call('cmd_shard', shard_index = 2, worker = 'w-other')
        self.assertEqual(code, 0)

        run = self.store.get_run('r1')
        statuses = {t['id']: t['status'] for t in run['tests']}
        self.assertEqual(statuses['other.only'], 'passed')
        self.assertEqual(statuses['fake.same'], 'pending')
        shard = run['shards'][1]
        self.assertEqual((shard['state'], shard['worker']), ('finished', 'w-other'))
        self.assertEqual(run['shards'][0]['state'], 'pending')
        self.assertIsNone(run['current'])
        self.assertEqual(run['counts']['passed'], 1)

    def test_shards_do_not_touch_the_run_record(self):
        self.plan()
        before = open(os.path.join(self.results, 'runs', 'r1', 'run.json')).read()
        self.call('cmd_shard', shard_index = 1)
        self.call('cmd_shard', shard_index = 2)
        after = open(os.path.join(self.results, 'runs', 'r1', 'run.json')).read()
        self.assertEqual(before, after)

    def test_full_flow_matches_a_single_process_run(self):
        self.plan()
        codes = [self.call('cmd_shard', shard_index = i)[0] for i in (1, 2, 3)]
        self.assertEqual(codes, [1, 0, 1])
        code, out = self.call('cmd_finalize')
        self.assertEqual(code, 1)

        run = self.store.get_run('r1')
        self.assertEqual((run['state'], run['result']), ('finished', 'failed'))

        single = os.path.join(self.tmp.name, 'single')
        with contextlib.redirect_stdout(io.StringIO()):
            fixtures.run_with_fake(single, self.work, run_id = 'solo')
        solo = Store(single).get_run('solo')
        self.assertEqual({t['id']: t['status'] for t in run['tests']},
                         {t['id']: t['status'] for t in solo['tests']})
        self.assertEqual(run['counts'], solo['counts'])

    def test_failure_details_are_available_per_test(self):
        self.plan()
        self.call('cmd_shard', shard_index = 1)
        result = self.store.get_result('r1', 'fake.differs')
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(os.path.exists(os.path.join(self.store.test_path('r1', 'fake.differs'), 'diff.txt')))

    @mock.patch('hub.selection.head_revision', return_value = 'abc1234567')
    def test_missing_shard_aborts_the_run_but_the_commit_stays_claimed(self, head):
        self.plan(trigger = 'push')
        self.call('cmd_shard', shard_index = 2)
        code, _ = self.call('cmd_finalize', trigger = 'push')
        self.assertEqual(code, 1)
        run = self.store.get_run('r1')
        self.assertEqual((run['state'], run['result']), ('aborted', 'aborted'))
        self.assertIn('Shard 1', run['message'])
        self.assertEqual(run['counts']['aborted'], 7)
        self.assertEqual(run['counts']['passed'], 1)
        self.assertEqual(self.store.last_tested('testbranch')['run'], 'r1')

    def test_environment_failure_marks_every_test_in_the_shard_as_error(self):
        self.plan()
        code, _ = self.call('cmd_shard', shard_index = 2, load_error = RuntimeError('DATADIR missing'))
        self.assertEqual(code, 1)
        result = self.store.get_result('r1', 'other.only')
        self.assertEqual(result['status'], 'error')
        self.assertIn('DATADIR missing', result['error']['traceback'])
        self.assertEqual(self.call('cmd_finalize')[0], 1)
        run = self.store.get_run('r1')
        self.assertEqual((run['state'], run['result']), ('finished', 'error'))
        self.assertIn('DATADIR missing', run['message'])

    def test_passing_push_run_records_last_tested(self):
        self.plan(trigger = 'push', all = False, tests = 'other')
        self.assertEqual(self.call('cmd_shard', shard_index = 1)[0], 0)
        self.assertEqual(self.call('cmd_finalize', trigger = 'push')[0], 0)
        run = self.store.get_run('r1')
        self.assertEqual(run['result'], 'passed')
        if run['revision']:
            self.assertEqual(self.store.last_tested('testbranch')['run'], 'r1')

    def test_run_with_no_impacted_tests_finishes_cleanly(self):
        empty = {'mode': 'impact', 'specs': [], 'changed_files': ['README.md'], 'base': 'abc',
                 'reasons': {}, 'unmapped': [], 'ignored': ['README.md'], 'full': False,
                 'unmatched_specs': [], 'tests': []}
        with mock.patch.object(runner, 'make_plan', return_value = empty):
            out = self.plan(all = False, tests = '')
        self.assertIn('SHARDS: 0', out)
        self.assertEqual(self.call('cmd_finalize')[0], 0)
        run = self.store.get_run('r1')
        self.assertEqual((run['state'], run['result']), ('finished', 'passed'))
        self.assertIn('No regression tests', run['message'])

    def test_plan_that_cannot_import_the_tests_closes_the_run(self):
        code, out = self.call('cmd_plan', broken_import = True)
        self.assertEqual(code, 1)
        self.assertIn('RUN_ID: r1', out)
        run = self.store.get_run('r1')
        self.assertEqual((run['state'], run['result']), ('finished', 'error'))
        self.assertEqual(self.call('cmd_finalize')[0], 1)

    def test_finalize_closes_a_run_that_was_queued_but_never_planned(self):
        self.store.save_run('r1', {'id': 'r1', 'state': 'queued', 'result': None, 'tests': [],
                                   'trigger': 'manual', 'branch': 'main'})
        self.assertEqual(self.call('cmd_finalize')[0], 1)
        run = self.store.get_run('r1')
        self.assertEqual((run['state'], run['result']), ('finished', 'error'))
        self.assertIn('before any test was selected', run['message'])

    def test_finalize_is_idempotent_and_handles_unknown_runs(self):
        self.plan(all = False, tests = 'other')
        self.call('cmd_shard', shard_index = 1)
        self.assertEqual(self.call('cmd_finalize')[0], 0)
        self.assertEqual(self.call('cmd_finalize')[0], 0)
        self.assertEqual(self.call('cmd_finalize', run_id = 'nope')[0], 2)
        self.assertEqual(self.call('cmd_shard', shard_index = 9)[0], 2)

    def test_console_log_is_shared_and_prefixed(self):
        self.plan(all = False, tests = 'other')
        self.call('cmd_shard', shard_index = 1)
        with open(os.path.join(self.results, 'runs', 'r1', 'runner.log')) as handle:
            text = handle.read()
        self.assertIn('[shard 1] [1/1] other.only', text)


if __name__ == '__main__':
    unittest.main()
