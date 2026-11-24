from datetime import datetime
from urllib.parse import urlencode


STATUS_LABEL = {'error': 'error', 'failed': 'failed', 'no_expected': 'no baseline',
                'aborted': 'aborted', 'running': 'running', 'pending': 'pending',
                'passed': 'passed', 'queued': 'queued', 'finished': 'finished'}


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S%z')
    except ValueError:
        return None


def clock(value):
    parsed = parse_time(value)
    return parsed.strftime('%d %b %H:%M:%S') if parsed else '-'


def ago(value):
    parsed = parse_time(value)
    if not parsed:
        return '-'
    seconds = int((datetime.now(parsed.tzinfo) - parsed).total_seconds())
    if seconds < 10:
        return 'just now'
    if seconds < 60:
        return '%ds ago' % seconds
    if seconds < 3600:
        return '%dm ago' % (seconds // 60)
    if seconds < 86400:
        return '%dh ago' % (seconds // 3600)
    return '%dd ago' % (seconds // 86400)


def span(seconds):
    if seconds is None:
        return '-'
    seconds = int(round(seconds))
    if seconds < 60:
        return '%ds' % seconds
    if seconds < 3600:
        return '%dm %02ds' % (seconds // 60, seconds % 60)
    return '%dh %02dm' % (seconds // 3600, (seconds % 3600) // 60)


def size(value):
    if value is None:
        return '-'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return ('%d %s' % (value, unit)) if unit == 'B' else ('%.1f %s' % (value, unit))
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
