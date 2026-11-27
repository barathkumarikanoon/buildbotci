"""What is really in progress for a repository.

A run's own record (`run.json`) says `running` or `queued` until the run writes its end. When Buildbot gives a build back
(state RETRY, for example after its worker went away) or loses it, nothing writes that end, and the record would say
`running` for ever. So every number and badge about "in progress" goes through here: the records are checked against what
Buildbot holds right now. One place, so the sidebar, the overview and the repository page always agree.

Per run the settled state is one of:
  running  Buildbot is executing a build of it
  waiting  Buildbot holds a request for it that no worker has taken (queued, or given back after RETRY)
  stalled  the record says it is in progress but Buildbot has nothing for it (it will not finish by itself)
When Buildbot cannot be asked, the recorded state is kept.
"""
import time

from django.conf import settings

from ciweb.runs import buildbot_client, fmt
from hub.store import RUNNING_STATES

GRACE_SECONDS = 90         # a run just made may not be visible in Buildbot yet
CACHE_SECONDS = 5          # the sidebar asks on every page; Buildbot is asked at most this often
_cache = {'at': 0.0, 'value': None}


def buildbot_live():
    """`buildbot_client.live_runs` or None when Buildbot cannot be asked, remembered for a few seconds (also a failure, so a
    master that is down costs one short wait, not one for every page)."""
    now = time.monotonic()
    if now - _cache['at'] < CACHE_SECONDS:
        return _cache['value']
    try:
        value = buildbot_client.live_runs(settings.BUILDBOT_URL, settings.BUILDBOT_USER, settings.BUILDBOT_PASS, timeout = 3)
    except buildbot_client.BuildbotError:
        value = None
    _cache.update(at = now, value = value)
    return value


def forget():
    _cache.update(at = 0.0, value = None)


def settled_state(run, live):
    """The state to show for `run`: its recorded one, or running / waiting / stalled when it is in progress."""
    state = run.get('state')
    if state not in RUNNING_STATES or live is None:
        return state
    run_id = run.get('id')
    if live['running'].get(run_id):
        return 'running'
    if live['waiting'].get(run_id):
        return 'waiting'
    parsed = fmt.parse_time(run.get('created') or run.get('started'))
    if parsed and (time.time() - parsed.timestamp()) < GRACE_SECONDS:
        return state
    return 'stalled'


def settle(runs, live = False):
    """Copies of `runs` with `state` settled (the recorded one stays in `recorded_state`). `live=False` asks Buildbot."""
    live = buildbot_live() if live is False else live
    return [dict(run, recorded_state = run.get('state'), state = settled_state(run, live)) for run in runs]


def summary(runs):
    """{'running', 'waiting', 'stalled', 'busy', 'text'} for settled runs; `busy` = running + waiting."""
    count = {name: sum(1 for run in runs if run.get('state') == name) for name in ('running', 'waiting', 'stalled')}
    count['running'] += sum(1 for run in runs if run.get('state') == 'queued')          # Buildbot could not be asked: queued stays queued
    parts = []
    if count['running']:
        parts.append('%d running' % count['running'])
    if count['waiting']:
        parts.append('%d waiting for a worker' % count['waiting'])
    if count['stalled']:
        parts.append('%d stalled' % count['stalled'])
    count['busy'] = count['running'] + count['waiting']
    count['text'] = ', '.join(parts)
    return count
