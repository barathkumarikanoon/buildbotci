from datetime import datetime
from urllib.parse import urlencode


STATUS_LABEL = {'error': 'error', 'failed': 'failed', 'no_expected': 'no baseline',
                'aborted': 'aborted', 'running': 'running', 'pending': 'pending',
                'passed': 'passed', 'queued': 'queued', 'finished': 'finished',
                'waiting': 'waiting for a worker', 'stalled': 'stalled'}


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S%z')
    except ValueError:
        return None


def clock(value):
    parsed = parse_time(value)
    return parsed.strftime('%d %B %H:%M:%S') if parsed else '-'


def plural(number, unit):
    return '%d %s%s' % (number, unit, '' if number == 1 else 's')


def ago(value):
    parsed = parse_time(value)
    if not parsed:
        return '-'
    seconds = int((datetime.now(parsed.tzinfo) - parsed).total_seconds())
    if seconds < 10:
        return 'just now'
    if seconds < 60:
        return '%s ago' % plural(seconds, 'second')
    if seconds < 3600:
        return '%s ago' % plural(seconds // 60, 'minute')
    if seconds < 86400:
        return '%s ago' % plural(seconds // 3600, 'hour')
    return '%s ago' % plural(seconds // 86400, 'day')


def span(seconds):
    if seconds is None:
        return '-'
    seconds = int(round(seconds))
    if seconds < 60:
        return plural(seconds, 'second')
    if seconds < 3600:
        return '%s %s' % (plural(seconds // 60, 'minute'), plural(seconds % 60, 'second'))
    return '%s %s' % (plural(seconds // 3600, 'hour'), plural((seconds % 3600) // 60, 'minute'))


def size(value):
    if value is None:
        return '-'
    for unit in ('bytes', 'kilobytes', 'megabytes', 'gigabytes'):
        if value < 1024 or unit == 'gigabytes':
            return ('%d %s' % (value, unit)) if unit == 'bytes' else ('%.1f %s' % (value, unit))
        value /= 1024.0


def label(status):
    return STATUS_LABEL.get(status, status)


def run_duration(run):
    start = parse_time(run.get('started'))
    end   = parse_time(run.get('finished')) if run.get('finished') else None
    if not start:
        return None
    end = end or datetime.now(start.tzinfo)
    return (end - start).total_seconds()


def with_query(url, **params):
    kept = {k: v for k, v in params.items() if v not in (None, '')}
    return url + ('?' + urlencode(kept) if kept else '')
