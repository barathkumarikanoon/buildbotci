"""Clean up what Buildbot still holds for repos (or schedulers, workers, branches) that are no longer configured.

Taking a repo out of the profiles (deleting or disabling it, lowering its number of workers, dropping a branch or
shrinking `max_shards`) only removes it from the *configuration*. Buildbot keeps its rows in the database: builders,
schedulers and workers stay listed as inactive, and build requests that were still waiting stay waiting for ever (they
would run by themselves if a repo with the same name came back, or at the next master start).

`reconcile` runs inside the master, after every reconfig and once when it starts, and makes the database match the
configuration that is now loaded:

* build requests that wait for a builder that is no longer configured are cancelled (their buildsets complete);
* schedulers that are no longer configured are deleted (they only hold state);
* workers that are no longer configured, not connected and without history are deleted;
* builders that are no longer configured and have no builds are deleted; a builder with builds keeps them (so old runs
  can still be read in Buildbot) unless its name is in `erase` (the web UI asks for that when a repo is deleted with
  "erase Buildbot's history").

Builds that are running on a builder that was removed are stopped, so nothing keeps running for a repo that is gone.
"""
from twisted.internet import defer
from twisted.python import log

from buildbot.process.results import CANCELLED

from hub.repos import erase_request_path, read_erase_requests, write_erase_requests

def delete_cascade(conn, metadata, table, condition, path = ()):
    """Delete the rows of `table` that match `condition`, and first the rows of every table whose foreign key to it is
    declared `ON DELETE CASCADE`. The database does this itself (PostgreSQL); doing it here too keeps SQLite, which does
    not enforce foreign keys, clean. References that only clear a column (`SET NULL`) are left alone."""
    import sqlalchemy as sa
    for child in metadata.tables.values():
        if child is table or child in path:
            continue
        for key in child.foreign_keys:
            if key.column.table is table and (key.ondelete or '').upper() == 'CASCADE':
                inner = sa.select(key.column).where(condition)
                delete_cascade(conn, metadata, child, key.parent.in_(inner), path + (table,))
    conn.execute(table.delete().where(condition))


def purge_rows(conn, model, configured_builders, configured_schedulers, configured_workers, erase = ()):
    """The database part of `reconcile`. `model` has the Buildbot tables (`buildbot.db.model.Model`).
    Returns {'schedulers': [...], 'workers': [...], 'builders': [...], 'erased': [...]} of the names it deleted."""
    import sqlalchemy as sa
    t, meta = model, model.metadata
    done = {'schedulers': [], 'workers': [], 'builders': [], 'erased': []}

    # builders first: deleting them takes their builds along, which decides which workers are still needed
    for row in conn.execute(sa.select(t.builders.c.id, t.builders.c.name)).fetchall():
        if row.name in configured_builders:
            continue
        has_masters = conn.execute(sa.select(sa.func.count()).select_from(t.builder_masters).where(t.builder_masters.c.builderid == row.id)).scalar()
        if has_masters:
            continue
        builds = conn.execute(sa.select(sa.func.count()).select_from(t.builds).where(t.builds.c.builderid == row.id)).scalar()
        if builds and row.name not in erase:
            continue
        delete_cascade(conn, meta, t.builders, t.builders.c.id == row.id)
        done['erased' if builds else 'builders'].append(row.name)

    for row in conn.execute(sa.select(t.schedulers.c.id, t.schedulers.c.name)).fetchall():
        if row.name in configured_schedulers:
            continue
        active = conn.execute(sa.select(sa.func.count()).select_from(t.scheduler_masters).where(t.scheduler_masters.c.schedulerid == row.id)).scalar()
        if active:
            continue
        delete_cascade(conn, meta, t.schedulers, t.schedulers.c.id == row.id)
        done['schedulers'].append(row.name)

    for row in conn.execute(sa.select(t.workers.c.id, t.workers.c.name)).fetchall():
        if row.name in configured_workers:
            continue
        connected = conn.execute(sa.select(sa.func.count()).select_from(t.connected_workers).where(t.connected_workers.c.workerid == row.id)).scalar()
        used = conn.execute(sa.select(sa.func.count()).select_from(t.builds).where(t.builds.c.workerid == row.id)).scalar()
        if connected or used:
            continue
        delete_cascade(conn, meta, t.workers, t.workers.c.id == row.id)
        done['workers'].append(row.name)

    if done['builders'] or done['erased']:
        # the buildsets of the requests that went with their builders
        delete_cascade(conn, meta, t.buildsets, ~t.buildsets.c.id.in_(sa.select(t.buildrequests.c.buildsetid)))
    return done


@defer.inlineCallbacks
def cancel_orphan_work(master, configured_builders):
    """Cancel waiting build requests and stop running builds of builders that are not configured. Returns
    (requests cancelled, builds stopped)."""
    names     = {b.id: b.name for b in (yield master.db.builders.getBuilders())}
    orphan    = {i for i, n in names.items() if n not in configured_builders}
    cancelled = stopped = 0
    if not orphan:
        return 0, 0

    waiting = [r for r in (yield master.db.buildrequests.getBuildRequests(complete = False)) if r.builderid in orphan]
    ids = [r.buildrequestid for r in waiting]
    if ids:
        yield master.data.updates.claimBuildRequests(ids)
        yield master.data.updates.completeBuildRequests(ids, CANCELLED)
        cancelled = len(ids)
        log.msg('cleanup: cancelled %d waiting build request(s) of removed builders: %s' % (
            cancelled, ', '.join(sorted({names[r.builderid] for r in waiting}))))

    for build in (yield master.db.builds.getBuilds(complete = False)):
        if build.builderid in orphan:
            try:
                yield master.data.control('stop', {'reason': 'The repo was removed from the CI'}, ('builds', build.id))
                stopped += 1
            except Exception as exc:
                log.msg('cleanup: could not stop build %s of %s: %s' % (build.id, names[build.builderid], exc))
    return cancelled, stopped


@defer.inlineCallbacks
def reconcile(master, snapshot_path = ''):
    """Make the database match the configuration the master has loaded. Never raises: a failure is logged and the
    next reconfig tries again. Returns what it did (for tests and the log)."""
    report = {'cancelled': 0, 'stopped': 0, 'schedulers': [], 'workers': [], 'builders': [], 'erased': []}
    try:
        config     = master.config
        builders   = {b.name for b in config.builders}
        schedulers = set(config.schedulers)
        workers    = {w.workername for w in config.workers}
        asked      = read_erase_requests(erase_request_path(snapshot_path)) if snapshot_path else []

        report['cancelled'], report['stopped'] = yield cancel_orphan_work(master, builders)

        from buildbot.db.model import Model
        done = yield master.db.pool.do_with_transaction(lambda conn: purge_rows(conn, Model, builders, schedulers, workers, set(asked)))
        report.update(done)
        for kind in ('builders', 'schedulers', 'workers', 'erased'):
            if done[kind]:
                log.msg('cleanup: deleted %s: %s' % (kind, ', '.join(done[kind])))
        if asked:
            # Handled requests are dropped. One whose builder is still configured waits: the web UI writes the request
            # a moment before it takes the repo out of the profiles.
            write_erase_requests(erase_request_path(snapshot_path), [n for n in asked if n in builders])
    except Exception as exc:
        log.err(exc, 'cleanup of removed repos')
    return report
