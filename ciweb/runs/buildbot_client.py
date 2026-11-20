from urllib.parse import urlparse

import requests


class BuildbotError(Exception):
    pass


def force_build(base_url, user, password, scheduler, params, timeout = 20):
    if not base_url:
        raise BuildbotError('REG_BUILDBOT_URL is not configured')

    base   = base_url.rstrip('/') + '/'
    parsed = urlparse(base)
    origin = '%s://%s' % (parsed.scheme, parsed.netloc)

    session = requests.Session()
    try:
        if user:
            login = session.get(base + 'auth/login', auth = (user, password),
                                timeout = timeout, headers = {'Origin': origin})
            if login.status_code >= 400:
                raise BuildbotError('Buildbot login failed (HTTP %d)' % login.status_code)

        payload = {'jsonrpc': '2.0', 'method': 'force', 'id': 1, 'params': params}
        reply = session.post(base + 'api/v2/forceschedulers/%s' % scheduler,
                             json = payload, timeout = timeout,
                             headers = {'Origin': origin, 'Referer': base})
    except requests.RequestException as exc:
        raise BuildbotError('Cannot reach Buildbot: %s' % exc)

    try:
        body = reply.json()
    except ValueError:
        raise BuildbotError('Unexpected Buildbot reply (HTTP %d)' % reply.status_code)

    if 'error' in body:
        error = body['error']
        message = error.get('message') if isinstance(error, dict) else error
        raise BuildbotError('Buildbot refused the build: %s' % message)
    if reply.status_code >= 400:
        raise BuildbotError('Buildbot returned HTTP %d' % reply.status_code)

    return body.get('result')
