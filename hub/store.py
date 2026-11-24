import json
import os
import re
import secrets
import tempfile
import time

ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')

RUNNING_STATES  = ('queued', 'running')
ALL_STATUSES    = ('passed', 'failed', 'error', 'no_expected', 'aborted', 'pending')
SHARD_FIELDS    = ('state', 'worker', 'build', 'started', 'finished', 'message')
FAILING_STATUS  = ('failed', 'error', 'no_expected', 'aborted')


def valid_id(value):
    return bool(value) and bool(ID_RE.match(value)) and '..' not in value


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def count_statuses(items):
    counts = {name: 0 for name in ALL_STATUSES}
    for item in items:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    counts['total'] = len(items)
    return counts


def write_json(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok = True)
    fd, tmp = tempfile.mkstemp(dir = directory, prefix = '.tmp-')
    with os.fdopen(fd, 'w', encoding = 'utf8') as handle:
        json.dump(data, handle, indent = 1, ensure_ascii = False)
    os.replace(tmp, path)


def read_json(path, default = None):
    try:
        with open(path, 'r', encoding = 'utf8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


class Store:
    def __init__(self, root):
        self.root      = os.path.abspath(os.path.expanduser(os.path.expandvars(root)))
        self.runs_dir  = os.path.join(self.root, 'runs')
        self.state_dir = os.path.join(self.root, 'state')
        os.makedirs(self.runs_dir, exist_ok = True)
        os.makedirs(self.state_dir, exist_ok = True)

    def new_run_id(self, tag):
        return '%s-%s' % (time.strftime('%Y%m%d-%H%M%S'), tag)

    def new_ui_run_id(self):
        return self.new_run_id('ui-%s' % secrets.token_hex(2))

    def run_path(self, run_id):
        if not valid_id(run_id):
            raise ValueError('bad run id')
        return os.path.join(self.runs_dir, run_id)

    def test_path(self, run_id, test_id):
        if not valid_id(test_id):
            raise ValueError('bad test id')
        return os.path.join(self.run_path(run_id), 'tests', test_id)

    def stop_path(self, run_id):
        return os.path.join(self.run_path(run_id), 'stop.json')

    def get_stop(self, run_id):
        return read_json(self.stop_path(run_id))

    def save_stop(self, run_id, who):
        data = {'by': who, 'at': now()}
        write_json(self.stop_path(run_id), data)
        return data

    def shard_path(self, run_id, index):
        return os.path.join(self.run_path(run_id), 'shards', '%d.json' % int(index))

    def save_shard(self, run_id, index, data):
        write_json(self.shard_path(run_id, index), data)

    def merge_shards(self, run):
        shards = run.get('shards')
        if not shards or run.get('state') not in RUNNING_STATES:
            return run
        by_id    = {t['id']: t for t in run.get('tests', [])}
        currents = []
        for shard in shards:
            data = read_json(self.shard_path(run['id'], shard['index']))
            if not data:
                continue
            for key in SHARD_FIELDS:
                shard[key] = data.get(key)
            for item in data.get('tests', []):
                target = by_id.get(item['id'])
                if target:
                    target['status']   = item['status']
                    target['duration'] = item['duration']
            if data.get('current'):
                currents.append(data['current'])
        run['current'] = ', '.join(currents) or None
        run['counts']  = count_statuses(run['tests'])
        return run

    def _load(self, name):
        run = read_json(os.path.join(self.runs_dir, name, 'run.json'))
        return self.merge_shards(run) if run else run

    def get_run(self, run_id):
        return self._load(os.path.basename(self.run_path(run_id)))

    def save_run(self, run_id, data):
        write_json(os.path.join(self.run_path(run_id), 'run.json'), data)

    def get_result(self, run_id, test_id):
        return read_json(os.path.join(self.test_path(run_id, test_id), 'result.json'))

    def list_runs(self, limit = 50, offset = 0, branch = None, result = None, state = None, requested_by = None,
                  recent = None):
        """One page of runs, newest first, and the total number of runs that match.
        Without a filter only the runs of the page are read. With a filter every run is read, unless `recent` limits
        the search to that many of the newest runs (enough for questions like 'what is running now')."""
        try:
            names = sorted((n for n in os.listdir(self.runs_dir)
                            if valid_id(n) and os.path.exists(os.path.join(self.runs_dir, n, 'run.json'))), reverse = True)
        except OSError:
            return [], 0

        if not (branch or result or state or requested_by):
            runs = [run for run in (self._load(name) for name in names[offset:offset + limit]) if run]
            return runs, len(names)

        if recent:
            names = names[:recent]
        runs  = []
        total = 0
        for name in names:
            run = self._load(name)
            if not run:
                continue
            if branch and run.get('branch') != branch:
                continue
            if result and run.get('result') != result:
                continue
            if state and run.get('state') != state:
                continue
            if requested_by and run.get('requested_by') != requested_by:
                continue
            total += 1
            if offset <= total - 1 < offset + limit:
                runs.append(run)
        return runs, total

    def prune(self, older_than_days, keep_latest = 50, dry_run = True):
        """Delete the folders of old, ended runs. Never touched: the newest `keep_latest` runs, runs that are queued or
        running, and the run that is the last scheduled test of a branch. Returns (names, bytes) of what was (or would be) removed."""
        import calendar
        import shutil
        protected = {info.get('run') for info in self.all_last_tested().values() if info.get('run')}
        names  = sorted((n for n in os.listdir(self.runs_dir) if valid_id(n)), reverse = True) if os.path.isdir(self.runs_dir) else []
        limit  = time.time() - older_than_days * 86400
        victims, size = [], 0
        for position, name in enumerate(names):
            if position < keep_latest or name in protected:
                continue
            run = self._load(name) or {}
            if run.get('state') in RUNNING_STATES:
                continue
            path = os.path.join(self.runs_dir, name)
            stamp = run.get('finished') or run.get('created')
            try:
                moment = calendar.timegm(time.strptime(stamp[:19], '%Y-%m-%dT%H:%M:%S')) if stamp else os.path.getmtime(path)
                if stamp and len(stamp) >= 24:
                    sign = 1 if stamp[19] == '+' else -1
                    moment -= sign * (int(stamp[20:22]) * 3600 + int(stamp[22:24]) * 60)
            except (ValueError, TypeError, IndexError, OSError):
                moment = os.path.getmtime(path)
            if moment > limit:
                continue
            for folder, _, files in os.walk(path):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(folder, f))
                    except OSError:
                        pass
            victims.append(name)
            if not dry_run:
                shutil.rmtree(path, ignore_errors = True)
        return victims, size

    def history(self, test_id, limit = 20, before = None):
        entries = []
        for name in sorted(os.listdir(self.runs_dir), reverse = True)[:400]:
            if before and name >= before:
                continue
            run = self._load(name)
            if not run:
                continue
            for item in run.get('tests', []):
                if item['id'] == test_id:
                    entries.append({'run': run['id'], 'status': item['status'],
                                    'branch': run.get('branch'),
                                    'revision': (run.get('revision') or '')[:8],
                                    'created': run.get('created')})
                    break
            if len(entries) >= limit:
                break
        return entries

    def catalog_path(self, branch):
        if not valid_id(branch):
            raise ValueError('bad branch')
        return os.path.join(self.root, 'catalogs', '%s.json' % branch)

    def catalog(self, branch):
        return read_json(self.catalog_path(branch), {'modules': {}, 'tests': []})

    def save_catalog(self, catalog, branch):
        write_json(self.catalog_path(branch), catalog)
        self.clear_catalog_error(branch)

    def catalog_error_path(self, branch):
        return self.catalog_path(branch)[:-len('.json')] + '.error.json'

    def save_catalog_error(self, branch, message):
        write_json(self.catalog_error_path(branch), {'message': message[:600], 'at': now()})

    def catalog_error(self, branch):
        return read_json(self.catalog_error_path(branch), None)

    def clear_catalog_error(self, branch):
        try:
            os.remove(self.catalog_error_path(branch))
        except FileNotFoundError:
            pass

    def last_tested(self, branch):
        data = read_json(os.path.join(self.state_dir, 'last_tested.json'), {})
        return data.get(branch)

    def set_last_tested(self, branch, revision, run_id):
        path = os.path.join(self.state_dir, 'last_tested.json')
        data = read_json(path, {})
        data[branch] = {'revision': revision, 'run': run_id, 'at': now()}
        write_json(path, data)

    def all_last_tested(self):
        return read_json(os.path.join(self.state_dir, 'last_tested.json'), {})
