"""Everything the CI does with the git clone of one repo profile.

The CI never switches branches or changes files in the clone it is pointed at. Each run, and each module build,
gets its own throwaway checkout made with `git worktree`; they share the clone's objects, so nothing is cloned again.
"""
import contextlib
import fcntl
import glob
import json
import os
import re
import shutil
import urllib.request

from hub import selection
from hub.store import valid_id


class GitRepo:
    def __init__(self, config, path = None):
        """`path` is a checkout to work in instead of the configured clone (the throwaway checkout of a build)."""
        self.config   = config
        self.source   = config.repo_dir            # the clone that holds the objects and the lock
        self.path     = os.path.abspath(path) if path else config.repo_dir
        self.checkout = config.checkout_name

    def git(self, *args, path = None):
        return selection._git(path or self.path, *args)

    def verify(self):
        """Stop with a clear message unless the configured clone is usable."""
        if not os.path.isdir(self.path):
            raise SystemExit('Code checkout not found: %s (repo profile %s; fix its folder in the web UI)' % (self.path, self.config.key))
        if self.git('rev-parse', '--git-dir').returncode != 0:
            raise SystemExit('Not a git repository: %s' % self.path)
        if os.path.basename(self.path) != self.checkout:
            raise SystemExit("The folder of repo profile %s must be named '%s' (its code is imported by that name), got: %s" % (
                self.config.key, self.checkout, os.path.basename(self.path)))

    @contextlib.contextmanager
    def lock(self):
        """Git commands on one clone are serialised with a lock file, so concurrent runs cannot collide."""
        common = selection._git(self.source, 'rev-parse', '--git-common-dir').stdout.decode().strip()
        if not os.path.isabs(common):
            common = os.path.join(self.source, common)
        with open(os.path.join(common, 'ci.lock'), 'w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def fetch_branch(self, branch):
        """`git fetch origin` for one branch; returns the commit of origin/<branch>."""
        if not valid_id(branch.replace('/', '_')):
            raise SystemExit('Bad branch name: %r' % branch)
        ref     = 'refs/remotes/origin/%s' % branch
        fetched = self.git('fetch', '--quiet', 'origin', '+refs/heads/%s:%s' % (branch, ref), path = self.source)
        if fetched.returncode != 0:
            raise SystemExit('git fetch origin failed for %s: %s' % (branch, fetched.stderr.decode().strip()))
        found = self.git('rev-parse', '--verify', '%s^{commit}' % ref, path = self.source)
        if found.returncode != 0:
            raise SystemExit('origin/%s does not exist' % branch)
        return found.stdout.decode().strip()

    def add_worktree(self, dest, revision):
        dest = os.path.abspath(dest)
        if os.path.basename(dest) != self.checkout:
            raise SystemExit("The checkout folder must be named '%s', got: %s" % (self.checkout, dest))
        src = self.source
        with self.lock():
            if self.git('cat-file', '-e', revision + '^{commit}', path = src).returncode != 0:
                self.git('fetch', '--quiet', 'origin', path = src)
                if self.git('cat-file', '-e', revision + '^{commit}', path = src).returncode != 0:
                    raise SystemExit('Commit %s is not in %s even after git fetch origin' % (revision, src))
            self.git('worktree', 'prune', path = src)
            if os.path.exists(os.path.join(dest, '.git')):
                for step in (['checkout', '--quiet', '-f', '--detach', revision], ['clean', '-fdxq']):
                    done = self.git(*step, path = dest)
                    if done.returncode != 0:
                        raise SystemExit('git %s failed in %s: %s' % (step[0], dest, done.stderr.decode().strip()))
            else:
                os.makedirs(os.path.dirname(dest), exist_ok = True)
                done = self.git('worktree', 'add', '--quiet', '--detach', '--force', dest, revision, path = src)
                if done.returncode != 0:
                    raise SystemExit('git worktree add failed: %s' % done.stderr.decode().strip())
        return dest

    def remove_worktree(self, dest):
        """Remove a checkout made by add_worktree: from git's list and from the disk, whatever state it is in
        (listed but half deleted, deleted but still listed, or not listed at all). Never fails if it is already gone."""
        dest = os.path.abspath(dest)
        if os.path.realpath(dest) == os.path.realpath(self.source):
            raise SystemExit('Refusing to remove the code checkout itself: %s' % dest)
        with self.lock():
            self.git('worktree', 'remove', '--force', dest, path = self.source)
            shutil.rmtree(dest, ignore_errors = True)
            self.git('worktree', 'prune', path = self.source)

    def head_revision(self):
        return selection.head_revision(self.path)

    def current_branch(self):
        done = self.git('rev-parse', '--abbrev-ref', 'HEAD')
        return done.stdout.decode().strip() if done.returncode == 0 else None

    # ---- checkouts left behind by builds that were killed ----

    def tree_pattern(self):
        return re.compile(r'^(?P<worker>[^/]+)/(?P<builder>%s|%s)/(?:run-(?P<number>\d+)/)?%s$' % (
            re.escape(self.config.run_builder), re.escape(self.config.shard_builder), re.escape(self.checkout)))

    def sweep(self, base, busy, dry_run = False):
        """Remove the checkouts that no running build owns: every `<worker>/<key>-run/run-N/<checkout>` and
        `<worker>/<key>-shard/<checkout>` under `base` (whether git lists them or not), plus empty run folders.
        `busy` is the set from `buildbot_in_use`. Returns the removed paths. Other checkouts are never touched."""
        base    = os.path.abspath(base)
        pattern = self.tree_pattern()
        found   = {}

        def note(path):
            rel = os.path.relpath(path, base)
            m = pattern.match(rel.replace(os.sep, '/'))
            if m and not rel.startswith('..'):
                found[path] = (m.group('worker'), m.group('builder'), int(m.group('number')) if m.group('number') else None)

        listed = self.git('worktree', 'list', '--porcelain', path = self.source).stdout.decode('utf8', 'replace').splitlines()
        for line in listed:
            if line.startswith('worktree '):
                note(os.path.abspath(line[len('worktree '):]))
        for run_dir in glob.glob(os.path.join(base, '*', self.config.run_builder, 'run-*')):
            note(os.path.join(run_dir, self.checkout))
        for tree in glob.glob(os.path.join(base, '*', self.config.shard_builder, self.checkout)):
            note(tree)

        removed = []
        for path, (worker, builder, number) in sorted(found.items()):
            if (worker, builder, number) in busy:
                continue
            if not dry_run:
                self.remove_worktree(path)
                if number is not None:
                    shutil.rmtree(os.path.dirname(path), ignore_errors = True)
            removed.append(path)
        return removed


def buildbot_in_use(url, run_builders = None, shard_builders = None, timeout = 10):
    """What Buildbot is running right now, as a set of (worker, builder, build number); for module builds the
    number is None, because a worker has one module checkout. None when Buildbot cannot be asked.
    `run_builders` / `shard_builders` are the builder names of the repos to look at (default: every `*-run` / `*-shard`)."""
    def fetch(path):
        with urllib.request.urlopen(url.rstrip('/') + '/api/v2/' + path, timeout = timeout) as reply:
            return json.load(reply)
    try:
        workers  = {w['workerid']: w['name'] for w in fetch('workers')['workers']}
        builders = {b['builderid']: b['name'] for b in fetch('builders')['builders']}
        running  = fetch('builds?complete__eq=false')['builds']
    except (OSError, ValueError, KeyError):
        return None

    def is_run(name):
        return name in run_builders if run_builders is not None else bool(name) and name.endswith('-run')

    def is_shard(name):
        return name in shard_builders if shard_builders is not None else bool(name) and name.endswith('-shard')

    busy = set()
    for build in running:
        builder = builders.get(build['builderid'])
        worker  = workers.get(build.get('workerid'))
        if is_run(builder):
            busy.add((worker, builder, int(build['number'])))
        elif is_shard(builder):
            busy.add((worker, builder, None))
    return busy
