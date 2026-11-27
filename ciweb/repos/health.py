"""Quick checks of a repo profile on the machine that serves the web UI. They are advice, never a reason to refuse a save:
the workers may be other machines."""
import os
import shutil

from hub.gitrepo import GitRepo

OK, WARN, BAD = 'ok', 'warn', 'bad'


def check(config, workers_dir = ''):
    """[(level, text)]"""
    found = []
    if not os.path.isdir(config.repo_dir):
        found.append((BAD, 'The clone folder %s does not exist on this machine.' % config.repo_dir))
    else:
        git = GitRepo(config)
        if git.git('rev-parse', '--git-dir').returncode != 0:
            found.append((BAD, '%s is not a git repository.' % config.repo_dir))
        else:
            found.append((OK, 'Clone found: %s' % config.repo_dir))
            origin = git.git('remote', 'get-url', 'origin')
            if origin.returncode != 0:
                found.append((BAD, 'The clone has no remote called origin; this system fetches from it.'))
            if os.path.basename(config.repo_dir) != config.checkout_name:
                found.append((WARN, 'The clone folder is called %s but test checkouts are called %s.' % (os.path.basename(config.repo_dir), config.checkout_name)))
    python = os.path.expanduser(os.path.expandvars(config.python))
    if os.path.isabs(python):
        found.append((OK, 'Python found: %s' % python) if os.access(python, os.X_OK) else (BAD, 'Python not found or not executable: %s' % python))
    elif shutil.which(python):
        found.append((OK, 'Python found on PATH: %s' % shutil.which(python)))
    else:
        found.append((WARN, '%s is not on this machine\'s PATH.' % python))
    return found


CONNECTED, BUSY, STOPPED, NOT_CREATED, UNKNOWN = 'connected', 'busy', 'stopped', 'not_created', 'unknown'
WORDS = {CONNECTED: 'Connected', BUSY: 'Busy', STOPPED: 'Not connected', NOT_CREATED: 'Not created', UNKNOWN: 'Unknown'}


def worker_rows(config, workers_dir, states = None):
    """One row for each worker of the repo: {name, state, label, hint}. `states` is buildbot_client.worker_states, or None
    when Buildbot could not be asked. A worker may live on another machine, so being connected wins over a missing folder."""
    rows = []
    for name in config.workers:
        here = bool(workers_dir) and os.path.exists(os.path.join(workers_dir, name, 'buildbot.tac'))
        known = (states or {}).get(name)
        if known and known['connected']:
            state, hint = (BUSY, 'running %d build(s)' % known['busy']) if known['busy'] else (CONNECTED, 'ready for work')
        elif states is None:
            state, hint = UNKNOWN, ('folder exists here' if here else 'no folder on this machine') + '; Buildbot could not be asked'
        elif here:
            state, hint = STOPPED, 'start it:  bin/workers start --worker %s' % name
        else:
            state, hint = NOT_CREATED, 'no folder on this machine; create it:  bin/create-workers %s' % config.key
        rows.append({'name': name, 'state': state, 'label': WORDS[state], 'hint': hint})
    return rows


def worker_summary(rows):
    """'2 of 3 connected' for a list page."""
    return '%d of %d connected' % (sum(1 for r in rows if r['state'] in (CONNECTED, BUSY)), len(rows))
