"""Starting, stopping and restarting the Buildbot workers that live on this machine.

A worker runs either under systemd (`buildbot-worker@<name>`, the server setup) or directly from its folder
(`buildbot-worker start <folder>`, a developer machine). `mode` picks one: 'auto' uses systemd when its unit template is
installed, 'systemd' and 'direct' force one. A worker that is running a build is not stopped unless `force` says so,
because stopping it ends that module's build (the run then marks its tests aborted).
"""
import os
import subprocess
import sys

from django.conf import settings

from ciweb.repos import decommission
from ciweb.runs import buildbot_client

ACTIONS = ('start', 'stop', 'restart', 'status')


def folder_of(name):
    return os.path.join(os.path.realpath(settings.WORKERS_DIR), name)


def pid_of(name):
    """The process number of the worker started from its folder (twistd.pid), or None when it is not running."""
    try:
        with open(os.path.join(folder_of(name), 'twistd.pid')) as handle:
            pid = int(handle.read().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid


def systemd_installed():
    try:
        return subprocess.run(['systemctl', 'cat', 'buildbot-worker@.service'], stdout = subprocess.DEVNULL,
                              stderr = subprocess.DEVNULL, timeout = 10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def use_systemd(mode):
    return mode == 'systemd' or (mode == 'auto' and systemd_installed())


def is_running(name, systemd):
    return decommission.systemd_unit_active(name) if systemd else pid_of(name) is not None


def connections():
    """{worker: {'connected', 'busy'}} as the master reports it, or None when Buildbot cannot be asked."""
    try:
        return buildbot_client.worker_states(settings.BUILDBOT_URL, settings.BUILDBOT_USER, settings.BUILDBOT_PASS)
    except buildbot_client.BuildbotError:
        return None


SAFE_ENVIRONMENT = ('PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LANGUAGE', 'TZ', 'TERM', 'TMPDIR')


def clean_environment():
    """What a worker process gets when this program starts it: no settings of the CI. The settings file holds the database
    password and the website secret, and the tests of the repositories run on the workers; the master hands each build
    step the few settings it needs (like the systemd unit, which does not load the settings file either)."""
    return {k: v for k, v in os.environ.items() if k in SAFE_ENVIRONMENT or k.startswith('LC_')}


def run_control(name, action, systemd):
    """Do one action on one worker. Returns (ok, short text)."""
    if systemd:
        command = ['systemctl', '--no-ask-password', action, 'buildbot-worker@%s' % name]
    else:
        program = os.path.join(os.path.dirname(sys.executable), 'buildbot-worker')
        if not os.path.exists(program):
            return False, 'buildbot-worker not found next to this Python'
        command = [program, action, folder_of(name)]
    try:
        done = subprocess.run(command, stdout = subprocess.PIPE, stderr = subprocess.STDOUT, timeout = 120,
                              env = None if systemd else clean_environment())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, 'failed (%s)' % exc
    lines = done.stdout.decode('utf8', 'replace').strip().splitlines()
    if done.returncode == 0:
        return True, ''
    text = lines[-1] if lines else 'exit code %d' % done.returncode
    if systemd:
        text += '; try:  sudo systemctl %s buildbot-worker@%s' % (action, name)
    return False, text


def link_text(name, running, linked):
    """', connected to the master' / ', NOT connected to the master (...)' for the status line; nothing when Buildbot cannot be asked."""
    if linked is None:
        return ', Buildbot could not be asked whether it is connected'
    known = linked.get(name)
    if known and known['connected']:
        return ', connected to the master' + (', running %d build(s)' % known['busy'] if known['busy'] else '')
    if running:
        return ', NOT connected to the master (read %s)' % os.path.join(folder_of(name), 'twistd.log')
    return ', not connected to the master'


def control(action, names, mode = 'auto', force = False, dry_run = False):
    """Start, stop, restart or report on `names`. Returns (report lines, number of workers that failed)."""
    if not settings.WORKERS_DIR:
        return ['CI_WORKERS_DIR is not set, so no workers can be found.'], 1
    installed = set(decommission.installed_workers())
    owners    = decommission.worker_owners()
    systemd   = use_systemd(mode)
    busy      = None
    linked    = connections() if action == 'status' else None
    if action in ('stop', 'restart') and not dry_run and any(n in installed and is_running(n, systemd) for n in names):
        from hub import gitrepo
        busy = gitrepo.buildbot_in_use(settings.BUILDBOT_URL)
    busy_names = {w for w, _, _ in busy} if busy else set()
    report, failed = [], 0
    for name in names:
        owner = owners.get(name)
        label = 'Worker %s%s' % (name, ' (%s)' % owner.key if owner else ' (not listed by any repository)')
        if name not in installed:
            report.append('%s: no folder on this machine%s.' % (label, ', nothing to stop' if action == 'stop' else '; bin/create-workers makes it' if owner else ''))
            failed += action in ('start', 'restart')
            continue
        running = is_running(name, systemd)
        how     = 'systemd' if systemd else 'pid %s' % pid_of(name) if running else 'direct'
        if action == 'status':
            report.append('%s: %s%s' % (label, 'running (%s)' % how if running else 'stopped', link_text(name, running, linked)))
        elif action == 'start' and running:
            report.append('%s: already running.' % label)
        elif action == 'stop' and not running:
            report.append('%s: already stopped.' % label)
        elif action in ('stop', 'restart') and running and busy is None and not force and not dry_run:
            report.append('%s: Buildbot could not be asked whether it is busy, left alone (--force to %s anyway).' % (label, action))
            failed += 1
        elif action in ('stop', 'restart') and name in busy_names and not force:
            report.append('%s: is running a build, left alone (stop the run first, or use --force).' % label)
            failed += 1
        elif dry_run:
            report.append('%s: would %s (%s).' % (label, action, 'systemd' if systemd else 'directly'))
        else:
            ok, text = run_control(name, action, systemd)
            report.append('%s: %s.' % (label, {'start': 'started', 'stop': 'stopped', 'restart': 'restarted'}[action] if ok else text))
            failed += not ok
    return report, failed


# ---- making worker folders ------------------------------------------------------------------------------------

PLACEHOLDERS = ('Your Name Here', 'Please put a description')


class WorkerError(Exception):
    pass


def info_is_placeholder(folder):
    """True when the worker still has the texts `buildbot-worker create-worker` writes (or no texts at all)."""
    for part in ('admin', 'host'):
        try:
            with open(os.path.join(folder, 'info', part), encoding = 'utf8') as handle:
                if any(mark in handle.read() for mark in PLACEHOLDERS):
                    return True
        except OSError:
            return True
    return False


def write_info(folder, name, profile):
    """What Buildbot's workers page shows for the worker. `admin`: `CI admin` (or CI_WORKER_ADMIN, if set). `host`: the
    worker's name and what it does, for example `indianlaw-w1 runs test modules of Indian Law (indianlaw)`.
    The worker sends both when it connects, so a running worker has to be restarted to show a change."""
    info = os.path.join(folder, 'info')
    os.makedirs(info, exist_ok = True)
    with open(os.path.join(info, 'admin'), 'w', encoding = 'utf8') as handle:
        handle.write((os.environ.get('CI_WORKER_ADMIN') or 'CI admin') + '\n')
    with open(os.path.join(info, 'host'), 'w', encoding = 'utf8') as handle:
        handle.write('%s runs test modules of %s (%s)\n' % (name, profile.name, profile.key))


def create_worker(profile, name):
    """Make the folder of one worker with `buildbot-worker create-worker`. Raises WorkerError with the reason."""
    password = os.environ.get('BUILDBOT_WORKER_PASSWORD')
    if not password:
        raise WorkerError('BUILDBOT_WORKER_PASSWORD is not set')
    master = os.environ.get('CI_WORKER_MASTER', 'localhost:%s' % os.environ.get('BUILDBOT_WORKER_PORT', '9989'))
    binary = os.path.join(os.path.dirname(sys.executable), 'buildbot-worker')
    folder = os.path.join(settings.WORKERS_DIR, name)
    try:
        done = subprocess.run([binary, 'create-worker', '--keepalive', '600', folder, master, name, password],
                              stdout = subprocess.PIPE, stderr = subprocess.STDOUT, timeout = 60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerError('could not run buildbot-worker (%s)' % exc)
    if done.returncode != 0:
        raise WorkerError('Creating %s failed: %s' % (name, done.stdout.decode('utf8', 'replace').strip()))
    write_info(folder, name, profile)


def create_missing(profile):
    """Make the worker folders of `profile` that do not exist yet on this machine. Returns (names created, problem text or '').
    Never raises: the website calls it after a save, and a problem there must not undo the save."""
    if not settings.WORKERS_DIR:
        return [], 'CI_WORKERS_DIR is not set, so no worker folders were made.'
    made = []
    for name in profile.worker_list():
        if os.path.exists(os.path.join(settings.WORKERS_DIR, name, 'buildbot.tac')):
            continue
        try:
            create_worker(profile, name)
        except WorkerError as exc:
            return made, str(exc)
        made.append(name)
    return made, ''


def start_missing(profile, mode = 'auto'):
    """Start the workers of `profile` that have a folder here and are not running. Returns (names started, problem texts).
    Never raises: the website calls it after a save, and a problem there must not undo the save."""
    systemd   = use_systemd(mode)
    installed = set(decommission.installed_workers())
    started, problems = [], []
    for name in profile.worker_list():
        if name not in installed or is_running(name, systemd):
            continue
        ok, text = run_control(name, 'start', systemd)
        if ok:
            started.append(name)
        else:
            problems.append('%s: %s' % (name, text))
    return started, problems


def create_and_start(profile, explicit = False):
    """The two steps after a repository is saved (the settings CI_AUTO_CREATE_WORKERS and CI_AUTO_START_WORKERS can switch
    them off) or when a person presses the button (`explicit`: always). Returns [(level, text)] for the page."""
    notes = []
    if explicit or settings.AUTO_CREATE_WORKERS:
        made, problem = create_missing(profile)
        if made:
            notes.append(('success', 'Created the worker folders %s on this machine.' % ', '.join(made)))
        if problem:
            notes.append(('warning', 'The workers could not all be created (%s). Run bin/create-workers %s.' % (problem, profile.key)))
    if explicit or settings.AUTO_START_WORKERS:
        started, problems = start_missing(profile)
        if started:
            notes.append(('success', 'Started the workers %s. They join the master in a few seconds.' % ', '.join(started)))
        for text in problems:
            notes.append(('warning', 'Could not start %s' % text))
    return notes
