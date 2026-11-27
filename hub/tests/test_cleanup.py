import json
import os
import tempfile
import unittest

from hub import repos

try:
    import sqlalchemy as sa
    from buildbot.db.model import Model
    from hub.buildbot import cleanup
except ImportError:                                    # Buildbot is not installed in this Python
    sa = None


def filler(column):
    kind = column.type.__class__.__name__.lower()
    if 'int' in kind or 'bool' in kind or 'float' in kind or 'numeric' in kind:
        return 0
    return 'x' * 40


def add(conn, table, **values):
    """Insert a row, filling every other required column with a dummy (SQLite does not check references)."""
    row = {c.name: filler(c) for c in table.columns
           if not c.nullable and c.default is None and c.server_default is None and not c.primary_key}
    row.update(values)
    conn.execute(table.insert().values(**row))


def count(conn, table, **where):
    query = sa.select(sa.func.count()).select_from(table)
    for name, value in where.items():
        query = query.where(table.c[name] == value)
    return conn.execute(query).scalar()


@unittest.skipIf(sa is None, 'Buildbot is not installed in this Python')
class PurgeRowsTest(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        Model.metadata.create_all(self.engine)
        self.conn = self.engine.connect()
        self.addCleanup(self.conn.close)
        t = Model
        add(self.conn, t.masters, id = 1, name = 'm', name_hash = 'h' * 40)
        for builder_id, name in ((1, 'keep-run'), (2, 'gone-run'), (3, 'gone-catalog'), (4, 'old-run')):
            add(self.conn, t.builders, id = builder_id, name = name, name_hash = name[:1] * 40 + str(builder_id))
        add(self.conn, t.builder_masters, id = 1, builderid = 1, masterid = 1)
        for sid, name in ((1, 'keep-nightly'), (2, 'gone-nightly'), (3, 'gone-shard-2')):
            add(self.conn, t.schedulers, id = sid, name = name, name_hash = str(sid) * 40)
        add(self.conn, t.scheduler_masters, schedulerid = 1, masterid = 1)
        for wid, name in ((1, 'keep-w1'), (2, 'gone-w1'), (3, 'gone-w2'), (4, 'gone-w3')):
            add(self.conn, t.workers, id = wid, name = name)
        add(self.conn, t.connected_workers, id = 1, masterid = 1, workerid = 3)
        # gone-run and old-run have history (builds); gone-run's worker is gone-w1
        add(self.conn, t.buildsets, id = 1)
        add(self.conn, t.buildrequests, id = 1, buildsetid = 1, builderid = 2)
        add(self.conn, t.builds, id = 1, number = 1, builderid = 2, buildrequestid = 1, workerid = 2, masterid = 1)
        add(self.conn, t.buildsets, id = 2)
        add(self.conn, t.buildrequests, id = 2, buildsetid = 2, builderid = 4)
        add(self.conn, t.builds, id = 2, number = 1, builderid = 4, buildrequestid = 2, workerid = 4, masterid = 1)
        add(self.conn, t.steps, id = 1, number = 0, name = 's', buildid = 1)
        self.conn.commit()

    def purge(self, erase = ()):
        with self.conn.begin():
            return cleanup.purge_rows(self.conn, Model, {'keep-run'}, {'keep-nightly'}, {'keep-w1'}, set(erase))

    def test_unconfigured_schedulers_are_deleted_and_configured_ones_stay(self):
        done = self.purge()
        self.assertEqual(sorted(done['schedulers']), ['gone-nightly', 'gone-shard-2'])
        self.assertEqual(count(self.conn, Model.schedulers), 1)

    def test_a_builder_without_builds_is_deleted_and_one_with_builds_keeps_its_history(self):
        done = self.purge()
        self.assertEqual(done['builders'], ['gone-catalog'])
        self.assertEqual(count(self.conn, Model.builders), 3)
        self.assertEqual(count(self.conn, Model.builds), 2)

    def test_a_configured_builder_is_never_touched(self):
        self.purge(erase = ['keep-run'])
        self.assertEqual(count(self.conn, Model.builders, name = 'keep-run'), 1)

    def test_erasing_a_builder_takes_its_builds_steps_and_requests_along(self):
        done = self.purge(erase = ['gone-run'])
        self.assertEqual(done['erased'], ['gone-run'])
        self.assertEqual(count(self.conn, Model.builds, builderid = 2), 0)
        self.assertEqual(count(self.conn, Model.steps), 0)
        self.assertEqual(count(self.conn, Model.buildrequests, builderid = 2), 0)
        self.assertEqual(count(self.conn, Model.builds, builderid = 4), 1)

    def test_workers_that_are_connected_or_have_history_stay(self):
        done = self.purge()
        self.assertEqual(done['workers'], [])
        done = self.purge(erase = ['gone-run', 'old-run'])
        self.assertEqual(sorted(done['workers']), ['gone-w1', 'gone-w3'])
        self.assertEqual(sorted(r.name for r in self.conn.execute(sa.select(Model.workers.c.name))), ['gone-w2', 'keep-w1'])

    def test_buildsets_left_without_requests_are_removed_with_their_builder(self):
        self.purge(erase = ['gone-run', 'old-run'])
        self.assertEqual(count(self.conn, Model.buildsets), 0)

    def test_running_it_again_changes_nothing(self):
        self.purge(erase = ['gone-run'])
        again = self.purge(erase = ['gone-run'])
        self.assertEqual(again, {'schedulers': [], 'workers': [], 'builders': [], 'erased': []})


class EraseRequestTest(unittest.TestCase):
    def test_the_request_file_round_trips_and_an_empty_list_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = repos.erase_request_path(os.path.join(tmp, 'profiles.json'))
            self.assertEqual(repos.read_erase_requests(path), [])
            repos.write_erase_requests(path, ['b-run', 'a-run', 'a-run'])
            self.assertEqual(repos.read_erase_requests(path), ['a-run', 'b-run'])
            repos.write_erase_requests(path, [])
            self.assertFalse(os.path.exists(path))
            repos.write_erase_requests(path, [])                       # nothing there: no error

    def test_a_damaged_file_is_read_as_no_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'cleanup.json')
            with open(path, 'w') as handle:
                handle.write('{not json')
            self.assertEqual(repos.read_erase_requests(path), [])
            with open(path, 'w') as handle:
                json.dump(['not', 'a', 'dict'], handle)
            self.assertEqual(repos.read_erase_requests(path), [])


if __name__ == '__main__':
    unittest.main()
