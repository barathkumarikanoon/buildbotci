from urllib.parse import urlparse

import requests


class BuildbotError(Exception):
    pass


def _open(base_url, user, password, timeout):
    if not base_url:
        raise BuildbotError('CI_BUILDBOT_URL is not configured')

    base   = base_url.rstrip('/') + '/'
    parsed = urlparse(base)
    origin = '%s://%s' % (parsed.scheme, parsed.netloc)
    session = requests.Session()
    headers = {'Origin': origin, 'Referer': base}
    try:
        if user:
            login = session.get(base + 'auth/login', auth = (user, password),
                                timeout = timeout, headers = {'Origin': origin})
            if login.status_code >= 400:
                raise BuildbotError('Buildbot login failed (HTTP %d)' % login.status_code)
    except requests.RequestException as exc:
        raise BuildbotError('Cannot reach Buildbot: %s' % exc)
    return session, base, headers


def _rpc(session, base, headers, path, method, params, timeout):
    """One JSON-RPC call. Returns (result, error message or None)."""
    payload = {'jsonrpc': '2.0', 'method': method, 'id': 1, 'params': params}
    try:
        reply = session.post(base + 'api/v2/' + path, json = payload, timeout = timeout, headers = headers)
    except requests.RequestException as exc:
        raise BuildbotError('Cannot reach Buildbot: %s' % exc)
    try:
        body = reply.json()
    except ValueError:
        raise BuildbotError('Unexpected Buildbot reply (HTTP %d)' % reply.status_code)
    if 'error' in body:
        error = body['error']
        return None, (error.get('message') if isinstance(error, dict) else str(error))
    if reply.status_code >= 400:
        return None, 'Buildbot returned HTTP %d' % reply.status_code
    return body.get('result'), None


def _get(session, base, path, timeout, **params):
    try:
        reply = session.get(base + 'api/v2/' + path, params = params, timeout = timeout)
    except requests.RequestException as exc:
        raise BuildbotError('Cannot reach Buildbot: %s' % exc)
    if reply.status_code >= 400:
        raise BuildbotError('Buildbot returned HTTP %d for %s' % (reply.status_code, path))
    try:
        return reply.json()
    except ValueError:
        raise BuildbotError('Unexpected Buildbot reply for %s' % path)


def worker_states(base_url, user, password, timeout = 10):
    """What Buildbot knows about its workers: {name: {'connected': bool, 'busy': number of running builds}}.
    A configured worker whose process is not running is listed as not connected."""
    session, base, _ = _open(base_url, user, password, timeout)
    workers = _get(session, base, 'workers', timeout).get('workers', [])
    busy    = {}
    for build in _get(session, base, 'builds', timeout, **{'complete__eq': 'false'}).get('builds', []):
        busy[build.get('workerid')] = busy.get(build.get('workerid'), 0) + 1
    return {w['name']: {'connected': bool(w.get('connected_to')), 'busy': busy.get(w.get('workerid'), 0)} for w in workers}


def live_runs(base_url, user, password, timeout = 3):
    """What Buildbot is doing for each run right now: {'running': {run_id: builds}, 'waiting': {run_id: requests}}.
    `running` are builds that are executing; `waiting` are requests nobody has taken yet (a run that is queued, or one
    whose build was given back with RETRY because its worker went away). A run in neither has nothing alive in Buildbot."""
    session, base, _ = _open(base_url, user, password, timeout)
    running, waiting = {}, {}
    for build in _get(session, base, 'builds', timeout, **{'complete__eq': 'false', 'property': 'run_id'}).get('builds', []):
        run_id = _property(build, 'run_id')
        if run_id:
            running[run_id] = running.get(run_id, 0) + 1
    pending = _get(session, base, 'buildrequests', timeout, **{'complete__eq': 'false', 'claimed__eq': 'false'})
    for request in pending.get('buildrequests', []):
        props  = _get(session, base, 'buildsets/%s/properties' % request['buildsetid'], timeout).get('properties') or [{}]
        value  = props[0].get('run_id')
        run_id = value[0] if isinstance(value, (list, tuple)) and value else value
        if run_id:
            waiting[run_id] = waiting.get(run_id, 0) + 1
    return {'running': running, 'waiting': waiting}


def force_build(base_url, user, password, scheduler, params, timeout = 20):
    session, base, headers = _open(base_url, user, password, timeout)
    result, error = _rpc(session, base, headers, 'forceschedulers/%s' % scheduler, 'force', params, timeout)
    if error:
        raise BuildbotError('Buildbot refused the build: %s' % error)
    return result


def _property(item, name):
    value = (item.get('properties') or {}).get(name)
    return value[0] if isinstance(value, (list, tuple)) and value else value


def stop_run(base_url, user, password, run_id, reason, parent_builder, timeout = 20):
    """Stop everything Buildbot has for one run: queued requests are cancelled and running builds
    (the run's own build, on `parent_builder`, and all of its module builds) are stopped. Stopping a build makes the worker
    send SIGTERM to the runner, which marks the unfinished tests `aborted`; the run's `finalize` step
    still runs and writes the final result.

    Returns {'builds': [...], 'requests': [...], 'parent_started': bool}. `parent_started` is False when
    the run's own build never started, so no runner will ever write the result."""
    session, base, headers = _open(base_url, user, password, timeout)
    names   = {b['builderid']: b['name'] for b in _get(session, base, 'builders', timeout).get('builders', [])}
    stopped = []
    cancelled = []
    errors  = []
    parent_started = False

    def mine_requests():
        found = []
        pending = _get(session, base, 'buildrequests', timeout, **{'complete__eq': 'false', 'claimed__eq': 'false'})
        for request in pending.get('buildrequests', []):
            props = _get(session, base, 'buildsets/%s/properties' % request['buildsetid'], timeout).get('properties') or [{}]
            value = props[0].get('run_id')
            if (value[0] if isinstance(value, (list, tuple)) and value else value) == run_id:
                found.append(request)
        return found

    def mine_builds():
        data = _get(session, base, 'builds', timeout, **{'complete__eq': 'false', 'property': 'run_id'})
        return [b for b in data.get('builds', []) if _property(b, 'run_id') == run_id]

    for _ in range(2):
        for request in mine_requests():
            if request['buildrequestid'] in cancelled:
                continue
            _, error = _rpc(session, base, headers, 'buildrequests/%s' % request['buildrequestid'], 'cancel',
                            {'reason': reason}, timeout)
            if error:
                errors.append(error)
            else:
                cancelled.append(request['buildrequestid'])
        for build in mine_builds():
            if names.get(build['builderid']) == parent_builder:
                parent_started = True
            if build['buildid'] in stopped:
                continue
            _, error = _rpc(session, base, headers, 'builds/%s' % build['buildid'], 'stop', {'reason': reason}, timeout)
            if error:
                errors.append(error)
            else:
                stopped.append(build['buildid'])

    if errors and not stopped and not cancelled:
        raise BuildbotError('Buildbot refused to stop the run: %s' % errors[0])
    return {'builds': stopped, 'requests': cancelled, 'parent_started': parent_started, 'errors': errors}


def stop_builders(base_url, user, password, builder_names, reason, wait = 30, timeout = 20):
    """Cancel every waiting build request and stop every running build of the given builders (all the work of a repo),
    then wait up to `wait` seconds for the builds to end, so their last steps (finalize, release checkout) still run.
    Returns {'requests': n, 'builds': n, 'still_running': n}."""
    import time
    session, base, headers = _open(base_url, user, password, timeout)
    names  = {b['builderid']: b['name'] for b in _get(session, base, 'builders', timeout).get('builders', [])}
    wanted = {i for i, n in names.items() if n in set(builder_names)}
    cancelled, stopped = set(), set()

    def running():
        data = _get(session, base, 'builds', timeout, **{'complete__eq': 'false'})
        return [b for b in data.get('builds', []) if b['builderid'] in wanted]

    for _ in range(3):
        pending = _get(session, base, 'buildrequests', timeout, **{'complete__eq': 'false', 'claimed__eq': 'false'})
        for request in pending.get('buildrequests', []):
            if request['builderid'] in wanted and request['buildrequestid'] not in cancelled:
                _, error = _rpc(session, base, headers, 'buildrequests/%s' % request['buildrequestid'], 'cancel', {'reason': reason}, timeout)
                if not error:
                    cancelled.add(request['buildrequestid'])
        for build in running():
            if build['buildid'] not in stopped:
                _, error = _rpc(session, base, headers, 'builds/%s' % build['buildid'], 'stop', {'reason': reason}, timeout)
                if not error:
                    stopped.add(build['buildid'])
        time.sleep(0.5)

    deadline = time.time() + wait
    left = running()
    while left and time.time() < deadline:
        time.sleep(1)
        left = running()
    return {'requests': len(cancelled), 'builds': len(stopped), 'still_running': len(left)}
