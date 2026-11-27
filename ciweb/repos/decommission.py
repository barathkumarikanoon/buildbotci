"""Taking a repo out of service: stop what it is doing, then remove what it leaves behind.

Removing a repo profile only removes it from the configuration; the master then drops the repo's schedulers, workers
and builders by itself (hub/buildbot/cleanup.py). This module does the parts that need the web UI's view: running
work, run records, worker folders on this machine, and the results.

Order matters. The work is stopped first and given a moment to end, so its last steps still run; only then is the
profile deleted, so the master never loses a builder under a build that is still finishing.
"""
import os
import shutil
import subprocess
import sys

from django.conf import settings

from hub import repos as hub_repos
from hub.gitrepo import GitRepo
from hub.store import RUNNING_STATES, Store, abort_unfinished
from ciweb.runs import buildbot_client


def builder_names(config):
    return [config.run_builder, config.shard_builder, config.catalog_builder]


def store_for(config):
    return Store(config.results_dir(settings.RESULTS_DIR))


def open_runs(config):
    """The runs of the repo that have not ended, according to their records."""
    runs, _ = store_for(config).list_runs(limit = 500)
    return [r for r in runs if r.get('state') in RUNNING_STATES]


def local_workers(config):
    """The worker folders of the repo that exist on this machine."""
    base = settings.WORKERS_DIR
    if not base:
        return []
    return [name for name in config.workers if os.path.isfile(os.path.join(base, name, 'buildbot.tac'))]


def describe(config):
    """What deleting the repo would touch right now (shown before the person confirms)."""
    root = config.results_dir(settings.RESULTS_DIR)
    return {'open_runs': len(open_runs(config)), 'workers': list(config.workers), 'local_workers': local_workers(config),
            'results_dir': root, 'has_results': os.path.isdir(root)}


def stop_work(config, reason):
    """Stop the repo's runs in Buildbot and close their records. Returns report lines."""
    report = []
    try:
        done = buildbot_client.stop_builders(settings.BUILDBOT_URL, settings.BUILDBOT_USER, settings.BUILDBOT_PASS,
                                             builder_names(config), reason)
        if done['requests'] or done['builds']:
            report.append('Buildbot: cancelled %d waiting request(s) and stopped %d running build(s).' % (done['requests'], done['builds']))
        if done['still_running']:
            report.append('Buildbot: %d build(s) were still ending after the wait; the master stops them when it drops the repository.' % done['still_running'])
    except buildbot_client.BuildbotError as exc:
        report.append('Buildbot could not be reached (%s). Waiting requests and running builds of this repository are cancelled by the master when it drops the repository, if it is running.' % exc)

    data = store_for(config)
    closed = 0
    for run in open_runs(config):
        data.save_run(run['id'], abort_unfinished(run, reason))
        closed += 1
    if closed:
        report.append('Closed %d run record(s) as aborted.' % closed)
    return report


def stop_worker_process(folder):
    """`buildbot-worker stop` with the program that belongs to this Python. Returns a short result text."""
    program = os.path.join(os.path.dirname(sys.executable), 'buildbot-worker')
    if not os.path.exists(program):
        return 'buildbot-worker not found next to this Python, not stopped'
    try:
        done = subprocess.run([program, 'stop', folder], stdout = subprocess.PIPE, stderr = subprocess.STDOUT, timeout = 60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 'could not stop it (%s)' % exc
    text = done.stdout.decode('utf8', 'replace').strip().splitlines()
    return 'stopped' if done.returncode == 0 else (text[-1] if text else 'was not running')


def remove_workers(config):
    """Stop the repo's workers that live on this machine and delete their folders and checkouts. Returns report lines."""
    base = settings.WORKERS_DIR
    if not base:
        return ['No worker folder is set (CI_WORKERS_DIR), so no worker folders were touched.']
    base   = os.path.realpath(base)
    report = []
    names  = local_workers(config)
    if names and os.path.isdir(config.repo_dir):
        try:
            GitRepo(config).sweep(base, busy = set())
        except (SystemExit, OSError) as exc:
            report.append('Checkouts of the repository could not all be removed from git (%s).' % exc)
    for name in names:
        folder = os.path.realpath(os.path.join(base, name))
        if os.path.dirname(folder) != base or os.path.basename(folder) != name:
            report.append('Worker %s: folder is outside the workers folder, left alone.' % name)
            continue
        outcome = stop_worker_process(folder)
        shutil.rmtree(folder, ignore_errors = True)
        report.append('Worker %s: %s, folder deleted.' % (name, outcome))
    if names:
        report.append('If these workers ran under systemd, also run: systemctl disable --now %s' % ' '.join('buildbot-worker@%s' % n for n in names))
    elif config.workers:
        report.append('None of the workers (%s) has a folder on this machine; workers on other machines must be stopped there.' % ', '.join(config.workers))
    return report


# ---- removing chosen workers only (the repository stays) ---------------------------------------------------------

def installed_workers():
    """Names of the worker folders on this machine (a folder with a buildbot.tac inside CI_WORKERS_DIR)."""
    base = settings.WORKERS_DIR
    if not base or not os.path.isdir(base):
        return []
    return sorted(n for n in os.listdir(base) if is_worker_folder(os.path.join(base, n)))


def is_worker_folder(folder):
    """A Buildbot worker folder, not a master: only a worker's buildbot.tac names a `workername`."""
    try:
        with open(os.path.join(folder, 'buildbot.tac'), encoding = 'utf8', errors = 'replace') as handle:
            return 'workername' in handle.read()
    except OSError:
        return False


def worker_owners():
    """{worker name: repository profile} for every worker that a profile still lists."""
    from ciweb.repos.models import RepoProfile
    return {name: profile for profile in RepoProfile.objects.all() for name in profile.worker_list()}


def systemd_unit_active(name):
    """True when `buildbot-worker@<name>` runs under systemd here: it would start the worker again after a stop."""
    try:
        done = subprocess.run(['systemctl', 'is-active', '--quiet', 'buildbot-worker@%s' % name], timeout = 10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def prune_checkouts(configs):
    """Make every clone forget the checkouts whose folders are gone (the ones inside a deleted worker folder)."""
    for config in configs:
        if os.path.isdir(config.repo_dir):
            git = GitRepo(config)
            try:
                with git.lock():
                    git.git('worktree', 'prune', path = git.source)
            except (SystemExit, OSError):
                pass


def remove_named_workers(names, mode, force = False, dry_run = False):
    """Stop and delete the worker folders `names` on this machine. Returns (report lines, names removed).

    mode: 'shrink' also takes a listed worker out of its repository profile (the master then drops it), 'keep' leaves
    the profile alone (bin/create-workers makes the folder again), '' refuses listed workers and deletes only the
    ones no profile lists any more. A worker that is running a build, or that systemd runs, is skipped (`force`
    ignores the build, never systemd, which would start it again). Nothing is deleted when `dry_run`."""
    base = settings.WORKERS_DIR
    if not base:
        return ['CI_WORKERS_DIR is not set, so no worker folders can be found.'], []
    base      = os.path.realpath(base)
    owners    = worker_owners()
    installed = set(installed_workers())
    busy      = None
    if installed & set(names):
        from hub import gitrepo
        busy = gitrepo.buildbot_in_use(settings.BUILDBOT_URL)
    busy_names = {w for w, _, _ in busy} if busy is not None else set()
    report, removed = [], []
    for name in names:
        owner  = owners.get(name)
        folder = os.path.realpath(os.path.join(base, name))
        if name not in installed:
            report.append('Worker %s: no folder on this machine, nothing to delete.' % name)
        elif os.path.dirname(folder) != base or os.path.basename(folder) != name:
            report.append('Worker %s: folder is outside the workers folder, left alone.' % name)
        elif owner and not mode:
            report.append('Worker %s: still serves repository %s, left alone. Add --shrink to remove it from the repository too, '
                          'or --keep-profile to delete only its folder.' % (name, owner.key))
        elif owner and mode == 'shrink' and not [n for n in owner.worker_list() if n not in names]:
            report.append('Worker %s: the last worker of %s, left alone. A repository needs one; delete the repository instead.' % (name, owner.key))
        elif systemd_unit_active(name):
            report.append('Worker %s: systemd runs it and would start it again. Run  sudo systemctl disable --now buildbot-worker@%s  first.' % (name, name))
        elif busy is None and not force:
            report.append('Worker %s: Buildbot could not be asked whether it is busy, left alone (--force to delete anyway).' % name)
        elif name in busy_names and not force:
            report.append('Worker %s: is running a build, left alone (stop the run first, or use --force).' % name)
        elif dry_run:
            report.append('Worker %s: would be stopped and its folder deleted%s.' % (name, '' if not owner else ' (repository %s)' % owner.key))
            removed.append(name)
        else:
            outcome = stop_worker_process(folder)
            shutil.rmtree(folder, ignore_errors = True)
            report.append('Worker %s: %s, folder deleted.' % (name, outcome))
            removed.append(name)

    if removed and not dry_run:
        prune_checkouts([p.to_config() for p in _profiles()])
        if mode == 'shrink':
            report += shrink_profiles(removed)
    return report, removed


def _profiles():
    from ciweb.repos.models import RepoProfile
    return RepoProfile.objects.all()


def shrink_profiles(removed):
    """Take the removed workers out of the profiles that list them and publish the change."""
    from ciweb.repos import publish
    report = []
    for profile in _profiles():
        listed = profile.worker_list()
        gone   = [n for n in listed if n in removed]
        if not gone:
            continue
        left = [n for n in listed if n not in removed]
        profile.worker_count = len(left)
        profile.worker_names = '' if left == ['%s-w%d' % (profile.key, i) for i in range(1, len(left) + 1)] else ','.join(left)
        profile.save()
        report.append('Repository %s now has %d worker(s): %s.' % (profile.key, len(left), ', '.join(left)))
    publish.publish()
    return report


def erase_results(config):
    root = os.path.realpath(config.results_dir(settings.RESULTS_DIR))
    top  = os.path.realpath(settings.RESULTS_DIR)
    if os.path.dirname(root) != top or os.path.basename(root) != config.key or not os.path.isdir(root):
        return ['No results folder to delete.']
    shutil.rmtree(root, ignore_errors = True)
    return ['Deleted the results folder %s.' % root]


def ask_master_to_erase_history(config):
    path = settings.PROFILES_FILE
    if not path:
        return []
    target = hub_repos.erase_request_path(path)
    hub_repos.write_erase_requests(target, hub_repos.read_erase_requests(target) + builder_names(config))
    return ["Asked the master to erase Buildbot's history of this repository's builds (the next reload does it)."]


def forget_erase_requests(config):
    """A repo that is added again must not inherit an old request to erase its history."""
    path = settings.PROFILES_FILE
    if not path:
        return
    target = hub_repos.erase_request_path(path)
    old    = hub_repos.read_erase_requests(target)
    keep   = [n for n in old if n not in set(builder_names(config))]
    if keep != old:
        hub_repos.write_erase_requests(target, keep)


def retire(profile, who, stop = True, workers = True, results = False, history = False):
    """Delete a repo profile and clean up after it. Returns report lines for the person who asked."""
    config = profile.to_config()
    report = []
    if stop:
        report += stop_work(config, 'The repository %s was deleted by %s' % (config.key, who))
    if history:
        report += ask_master_to_erase_history(config)
    name = profile.name
    profile.delete()
    from ciweb.repos import publish
    publish.publish()
    report.append('Deleted the repository profile %s. The master drops its schedulers, builders and workers within seconds.' % name)
    if workers:
        report += remove_workers(config)
    if results:
        report += erase_results(config)
    else:
        report.append('Its results are kept in %s.' % config.results_dir(settings.RESULTS_DIR))
    return report
