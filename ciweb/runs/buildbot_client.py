from urllib.parse import urlparse

import requests


class BuildbotError(Exception):
    pass


def _open(base_url, user, password, timeout):
    if not base_url:
        raise BuildbotError('REG_BUILDBOT_URL is not configured')

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


def force_build(base_url, user, password, scheduler, params, timeout = 20):
    session, base, headers = _open(base_url, user, password, timeout)
    result, error = _rpc(session, base, headers, 'forceschedulers/%s' % scheduler, 'force', params, timeout)
    if error:
        raise BuildbotError('Buildbot refused the build: %s' % error)
    return result


def _property(item, name):
    value = (item.get('properties') or {}).get(name)
    return value[0] if isinstance(value, (list, tuple)) and value else value


def stop_run(base_url, user, password, run_id, reason, timeout = 20):
    """Stop everything Buildbot has for one run: queued requests are cancelled and running builds
    (the run's own build and all of its module builds) are stopped. Stopping a build makes the worker
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
            if names.get(build['builderid']) == 'regression':
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
