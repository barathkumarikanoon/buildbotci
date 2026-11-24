import tempfile
import unittest

from hub.store import Store


def make_runs(store, count, **extra):
    for i in range(count):
        run_id = '20260101-%06d-ui-aaaa' % i
        run = {'id': run_id, 'state': 'finished', 'result': 'passed', 'branch': 'main', 'requested_by': 'a', 'tests': []}
        run.update(extra(i) if callable(extra) else extra)
        store.save_run(run_id, run)


class ListRunsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)

    def test_pages_are_newest_first_and_the_total_is_the_real_number_of_runs(self):
        make_runs(self.store, 75)
        first, total = self.store.list_runs(limit = 30, offset = 0)
        second, _ = self.store.list_runs(limit = 30, offset = 30)
        third, _ = self.store.list_runs(limit = 30, offset = 60)
        self.assertEqual(total, 75)
        self.assertEqual([len(first), len(second), len(third)], [30, 30, 15])
        self.assertEqual(first[0]['id'], '20260101-000074-ui-aaaa')
        self.assertEqual(third[-1]['id'], '20260101-000000-ui-aaaa')
        self.assertEqual(len({r['id'] for r in first + second + third}), 75)

    def test_a_folder_without_a_run_record_is_not_counted(self):
        import os
        make_runs(self.store, 3)
        os.makedirs(os.path.join(self.store.runs_dir, '20260101-999999-ui-zzzz'))
        runs, total = self.store.list_runs(limit = 10)
        self.assertEqual((len(runs), total), (3, 3))

    def test_filters_count_only_the_matching_runs(self):
        make_runs(self.store, 40, branch = 'main')
        for i in range(5):
            self.store.save_run('20260102-%06d-ui-bbbb' % i, {'id': '20260102-%06d-ui-bbbb' % i, 'state': 'finished',
                                                              'result': 'failed', 'branch': 'dev', 'requested_by': 'b', 'tests': []})
        runs, total = self.store.list_runs(limit = 3, branch = 'dev')
        self.assertEqual((len(runs), total), (3, 5))
        self.assertEqual(self.store.list_runs(result = 'passed')[1], 40)
        self.assertEqual(self.store.list_runs(requested_by = 'b')[1], 5)

    def test_the_recent_window_only_looks_at_the_newest_runs(self):
        make_runs(self.store, 50, state = 'finished')
        self.store.save_run('20250101-000000-ui-old1', {'id': '20250101-000000-ui-old1', 'state': 'running', 'tests': []})
        self.assertEqual(self.store.list_runs(state = 'running')[1], 1)
        self.assertEqual(self.store.list_runs(state = 'running', recent = 10)[1], 0)
        newest = self.store.list_runs(limit = 1)[0][0]['id']
        self.store.save_run(newest, dict(self.store.get_run(newest), state = 'running'))
        self.assertEqual(self.store.list_runs(state = 'running', recent = 10)[1], 1)


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)

    def add(self, name, created, state = 'finished'):
        self.store.save_run(name, {'id': name, 'state': state, 'created': created, 'finished': created if state == 'finished' else None, 'tests': []})
        import os
        with open(os.path.join(self.store.run_path(name), 'big.bin'), 'wb') as handle:
            handle.write(b'x' * 1000)

    def test_only_old_ended_runs_beyond_the_newest_are_removed(self):
        self.add('20250101-000001-old1', '2025-01-01T10:00:00+0530')
        self.add('20250101-000002-old2', '2025-01-01T11:00:00+0530')
        self.add('20250101-000003-run1', '2025-01-01T12:00:00+0530', state = 'running')
        self.add('20260101-000004-new1', '2099-01-01T10:00:00+0530')
        names, size = self.store.prune(30, keep_latest = 1, dry_run = True)
        self.assertEqual(sorted(names), ['20250101-000001-old1', '20250101-000002-old2'])
        self.assertGreater(size, 2000)
        self.assertEqual(len(self.store.list_runs()[0]), 4)

    def test_nothing_is_deleted_without_dry_run_being_switched_off(self):
        self.add('20250101-000001-old1', '2025-01-01T10:00:00+0530')
        self.store.prune(30, keep_latest = 0, dry_run = True)
        self.assertEqual(self.store.list_runs()[1], 1)
        self.store.prune(30, keep_latest = 0, dry_run = False)
        self.assertEqual(self.store.list_runs()[1], 0)

    def test_the_last_scheduled_run_of_a_branch_is_kept(self):
        self.add('20250101-000001-old1', '2025-01-01T10:00:00+0530')
        self.store.set_last_tested('main', 'abcdef1234', '20250101-000001-old1')
        self.assertEqual(self.store.prune(30, keep_latest = 0, dry_run = False)[0], [])
        self.assertEqual(self.store.list_runs()[1], 1)

    def test_the_newest_runs_are_always_kept(self):
        for i in range(5):
            self.add('2025010%d-00000%d-old%d' % (i + 1, i, i), '2025-01-0%dT10:00:00+0530' % (i + 1))
        names, _ = self.store.prune(30, keep_latest = 3, dry_run = True)
        self.assertEqual(len(names), 2)


class StorePathTest(unittest.TestCase):
    def test_the_results_folder_may_be_written_with_a_tilde_or_home(self):
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'HOME': home}):
            self.assertEqual(Store('~/results').root, os.path.join(home, 'results'))
            self.assertEqual(Store('$HOME/other').root, os.path.join(home, 'other'))
            self.assertTrue(os.path.isdir(os.path.join(home, 'results', 'runs')))
            self.assertFalse(os.path.exists(os.path.join(os.getcwd(), '~')))


if __name__ == '__main__':
    unittest.main()
