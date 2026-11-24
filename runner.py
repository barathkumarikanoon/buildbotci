import argparse
import contextlib
import fcntl
import glob
import inspect
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

def expand_path(value):
    """A folder or file given as ~/x or $HOME/x (as it may be written in the settings file) or as an absolute path."""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(value)))


HERE = os.path.dirname(os.path.abspath(__file__))
REPO = expand_path(os.environ.get('REG_REPO_DIR') or os.path.join(os.path.dirname(HERE), 'indianlaw'))

PENDING = 'pending'
ABORTED = 'aborted'
MAX_HUNK_SUMMARY = 1000


def use_repo(repo_dir = None):
    global REPO
    if repo_dir:
        REPO = expand_path(repo_dir)
    if not os.path.isdir(REPO):
        raise SystemExit('Code checkout not found: %s (use --repo-dir or REG_REPO_DIR)' % REPO)
    if os.path.basename(REPO) != 'indianlaw':
        raise SystemExit("Checkout directory must be named 'indianlaw' (code imports indianlaw.*), got: %s" % os.path.basename(REPO))
    from hub import selection
    if selection._git(REPO, 'rev-parse', '--git-dir').returncode != 0:
        raise SystemExit('Not a git repository: %s' % REPO)


def ensure_paths(repo_dir = None):
    use_repo(repo_dir)
    regressions = os.path.join(REPO, 'regressions')
    wanted = [HERE, regressions, os.path.join(REPO, 'website'), REPO, os.path.dirname(REPO)]
    sys.path[:] = wanted + [p for p in sys.path if p not in wanted]
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pylaw.settings')
    os.environ['OMP_NUM_THREADS'] = '1'
    os.chdir(regressions)


@contextlib.contextmanager
def repo_lock():
    from hub import selection
    common = selection._git(REPO, 'rev-parse', '--git-common-dir').stdout.decode().strip()
    if not os.path.isabs(common):
        common = os.path.join(REPO, common)
    with open(os.path.join(common, 'ci.lock'), 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def fetch_branch(branch):
    from hub import selection
    from hub.store import valid_id
    if not valid_id(branch):
        raise SystemExit('Bad branch name: %r' % branch)
    ref = 'refs/remotes/origin/%s' % branch
    fetched = selection._git(REPO, 'fetch', '--quiet', 'origin', '+refs/heads/%s:%s' % (branch, ref))
    if fetched.returncode != 0:
        raise SystemExit('git fetch origin failed for %s: %s' % (branch, fetched.stderr.decode().strip()))
    found = selection._git(REPO, 'rev-parse', '--verify', '%s^{commit}' % ref)
    if found.returncode != 0:
        raise SystemExit('origin/%s does not exist' % branch)
    return found.stdout.decode().strip()


def add_worktree(dest, revision):
    from hub import selection
    dest = os.path.abspath(dest)
    if os.path.basename(dest) != 'indianlaw':
        raise SystemExit("The checkout folder must be named 'indianlaw', got: %s" % dest)
    with repo_lock():
        if selection._git(REPO, 'cat-file', '-e', revision + '^{commit}').returncode != 0:
            selection._git(REPO, 'fetch', '--quiet', 'origin')
            if selection._git(REPO, 'cat-file', '-e', revision + '^{commit}').returncode != 0:
                raise SystemExit('Commit %s is not in %s even after git fetch origin' % (revision, REPO))
        selection._git(REPO, 'worktree', 'prune')
        if os.path.exists(os.path.join(dest, '.git')):
            for step in (['checkout', '--quiet', '-f', '--detach', revision], ['clean', '-fdxq']):
                done = selection._git(dest, *step)
                if done.returncode != 0:
                    raise SystemExit('git %s failed in %s: %s' % (step[0], dest, done.stderr.decode().strip()))
        else:
            os.makedirs(os.path.dirname(dest), exist_ok = True)
            done = selection._git(REPO, 'worktree', 'add', '--quiet', '--detach', '--force', dest, revision)
            if done.returncode != 0:
                raise SystemExit('git worktree add failed: %s' % done.stderr.decode().strip())
    return dest


def remove_worktree(dest):
    """Remove a checkout made by add_worktree: from git's list and from the disk, whatever state it is in
    (listed but half deleted, deleted but still listed, or not listed at all). Never fails if it is already gone."""
    from hub import selection
    dest = os.path.abspath(dest)
    if os.path.realpath(dest) == os.path.realpath(REPO):
        raise SystemExit('Refusing to remove the code checkout itself: %s' % dest)
    with repo_lock():
        selection._git(REPO, 'worktree', 'remove', '--force', dest)
        shutil.rmtree(dest, ignore_errors = True)
        selection._git(REPO, 'worktree', 'prune')


TREE_RE = re.compile(r'^(?P<worker>[^/]+)/(?P<builder>regression|regression-shard)/(?:run-(?P<number>\d+)/)?indianlaw$')


def buildbot_in_use(url, timeout = 10):
    """What Buildbot is running right now, as a set of (worker, builder, build number); for the module builds the
    number is None, because a worker has one module checkout. None when Buildbot cannot be asked."""
    import json
    import urllib.request

    def fetch(path):
        with urllib.request.urlopen(url.rstrip('/') + '/api/v2/' + path, timeout = timeout) as reply:
            return json.load(reply)
    try:
        workers  = {w['workerid']: w['name'] for w in fetch('workers')['workers']}
        builders = {b['builderid']: b['name'] for b in fetch('builders')['builders']}
        running  = fetch('builds?complete__eq=false')['builds']
    except (OSError, ValueError, KeyError):
        return None
    busy = set()
    for build in running:
        builder = builders.get(build['builderid'])
        worker  = workers.get(build.get('workerid'))
        if builder == 'regression':
            busy.add((worker, builder, int(build['number'])))
        elif builder == 'regression-shard':
            busy.add((worker, builder, None))
    return busy


def sweep_trees(base, busy, dry_run = False):
    """Remove the checkouts that no running build owns: every `<worker>/regression/run-N/indianlaw` and
    `<worker>/regression-shard/indianlaw` under `base` (whether git lists them or not), plus empty run folders.
    `busy` is the set from buildbot_in_use. Returns the removed paths. Other checkouts of the repository are never touched."""
    from hub import selection
    base    = os.path.abspath(base)
    found   = {}

    def note(path):
        rel = os.path.relpath(path, base)
        m = TREE_RE.match(rel.replace(os.sep, '/'))
        if m and not rel.startswith('..'):
            found[path] = (m.group('worker'), m.group('builder'), int(m.group('number')) if m.group('number') else None)

    listed = selection._git(REPO, 'worktree', 'list', '--porcelain').stdout.decode('utf8', 'replace').splitlines()
    for line in listed:
        if line.startswith('worktree '):
            note(os.path.abspath(line[len('worktree '):]))
    for run_dir in glob.glob(os.path.join(base, '*', 'regression', 'run-*')):
        note(os.path.join(run_dir, 'indianlaw'))
    for tree in glob.glob(os.path.join(base, '*', 'regression-shard', 'indianlaw')):
        note(tree)

    removed = []
    for path, owner in sorted(found.items()):
        worker, builder, number = owner
        if (worker, builder, number) in busy:
            continue
        if not dry_run:
            remove_worktree(path)
            if number is not None:
                shutil.rmtree(os.path.dirname(path), ignore_errors = True)
        removed.append(path)
    return removed


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()

    def isatty(self):
        return False


class Prefixed:
    def __init__(self, stream, prefix):
        self.stream  = stream
        self.prefix  = prefix
        self.pending = ''

    def write(self, text):
        self.pending += text
        lines = self.pending.split('\n')
        self.pending = lines.pop()
        if lines:
            self.stream.write(''.join('%s%s\n' % (self.prefix, line) for line in lines))
            self.stream.flush()
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


class Terminated(BaseException):
    pass


def on_sigterm(signum, frame):
    raise Terminated()


def build_catalog(regression):
    from hub.store import now
    from hub import selection

    if not hasattr(regression, 'MODULES'):
        raise RuntimeError('This branch has no regression.MODULES in regressions/regression.py, so its tests cannot be listed. '
                           'The regression.py change has not been merged into it yet.')

    tests   = []
    modules = {}
    for name, (cls, _) in regression.MODULES.items():
        modules[name] = {'class': cls.__name__,
                         'source': os.path.relpath(inspect.getsourcefile(cls), REPO)}
        for tname in regression.get_class_tests(cls):
            func   = getattr(cls, tname)
            takes  = 'outhandle' in inspect.signature(func).parameters
            output = regression.output_filename(tname) if takes else None
            doc    = (inspect.getdoc(func) or '').split('\n')[0]
            tests.append({'id': '%s.%s' % (name, tname), 'module': name,
                          'name': tname, 'output': output, 'doc': doc,
                          'has_expected': bool(output) and os.path.exists(os.path.join('expected', output))})
    return {'generated': now(), 'revision': selection.head_revision(REPO),
            'modules': modules, 'tests': tests}


def current_branch():
    from hub import selection
    p = selection._git(REPO, 'rev-parse', '--abbrev-ref', 'HEAD')
    return p.stdout.decode().strip() if p.returncode == 0 else None


def make_plan(args, catalog, store):
    from hub import selection

    specs = selection.split_specs(args.tests)
    plan  = {'mode': None, 'specs': specs, 'changed_files': [], 'base': None,
             'reasons': {}, 'unmapped': [], 'ignored': [], 'full': False,
             'unmatched_specs': [], 'tests': []}

    if specs:
        plan['mode'] = 'all' if specs == ['all'] else 'explicit'
        plan['tests'], plan['unmatched_specs'] = selection.expand_specs(specs, catalog)
        return plan

    if args.all:
        plan['mode'] = 'all'
        plan['tests'] = [t['id'] for t in catalog['tests']]
        return plan

    plan['mode'] = 'impact'
    branch = args.branch or current_branch()
    last   = store.last_tested(branch) if store else None
    base   = args.base or selection.resolve_base(REPO, (last or {}).get('revision'))
    plan['base'] = base
    if not base:
        plan['full'] = True
        plan['tests'] = [t['id'] for t in catalog['tests']]
        plan['reasons'] = {i: ['no base revision to compare against'] for i in plan['tests']}
        return plan

    files  = selection.changed_files(REPO, base)
    impact = selection.load_impact_map(os.path.join(HERE, 'hub', 'impact_map.json'))
    result = selection.impacted(files, impact, catalog)
    plan.update({'changed_files': files, 'reasons': result['reasons'],
                 'unmapped': result['unmapped'], 'ignored': result['ignored'],
                 'full': result['full'], 'tests': result['tests']})
    return plan


def count_statuses(items):
    from hub.store import count_statuses as counted
    return counted(items)


def assign_shards(modules, limit):
    count  = max(1, min(len(modules), limit))
    shards = [{'index': i + 1, 'modules': modules[i::count]} for i in range(count)]
    return [dict(s, state = 'pending', worker = None, build = None) for s in shards if s['modules']]


def initial_run(args, run_id, previous, branch, revision):
    from hub.store import now
    return {
        'id': run_id, 'state': 'running', 'result': None,
        'trigger': args.trigger or previous.get('trigger') or 'cli',
        'branch': branch, 'revision': revision, 'profile': args.profile,
        'requested_by': args.requested_by or previous.get('requested_by'),
        'reason': args.reason or previous.get('reason'),
        'build': {'number': args.build_number, 'url': args.build_url},
        'created': previous.get('created') or now(), 'started': now(), 'finished': None,
        'selection': None, 'tests': [], 'counts': count_statuses([]),
        'current': None, 'message': None,
    }


def selection_record(plan):
    record = {k: plan[k] for k in ('mode', 'specs', 'base', 'changed_files',
                                   'unmapped', 'ignored', 'full', 'unmatched_specs')}
    record['reasons'] = {i: plan['reasons'].get(i, []) for i in plan['tests']}
    return record


def make_loader(regression, storage, srcs, env_error = None):
    objects = {}
    failed  = {}

    def get_obj(module):
        if env_error:
            raise env_error
        if module in failed:
            raise failed[module]
        if module not in objects:
            try:
                objects[module] = regression.load_regress_objs(storage, srcs, [module])[module]
            except Exception as exc:
                failed[module] = exc
                raise
        return objects[module]

    return get_obj


def exception_info(exc):
    frames = []
    own = os.path.abspath(__file__)
    for frame in traceback.extract_tb(exc.__traceback__):
        if not frames and os.path.abspath(frame.filename) == own:
            continue
        path = frame.filename
        if path.startswith(REPO + os.sep):
            path = os.path.relpath(path, REPO)
        frames.append({'file': path, 'line': frame.lineno, 'func': frame.name,
                       'code': frame.line})
    return {'type': type(exc).__name__, 'message': str(exc),
            'traceback': ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            'frames': frames}


def human_size(count):
    value = float(count)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return ('%d %s' % (value, unit)) if unit == 'B' else ('%.1f %s' % (value, unit))
        value /= 1024.0


def saved_files_lines(directory, run_id, test_id, status):
    if status == 'passed':
        return []
    lines = []
    for name, label in (('diff.txt', 'full diff'), ('expected', 'expected'), ('actual', 'actual')):
        path = os.path.join(directory, name)
        size = file_size(path)
        if size is not None:
            lines.append('    %-9s %s  (%s)' % (label + ':', path, human_size(size)))
    ui = os.environ.get('REG_UI_URL')
    if ui and lines:
        page = '%s/run/%s/test/%s' % (ui.rstrip('/'), run_id, test_id)
        lines.append('    open it:  %s' % page)
        if os.path.exists(os.path.join(directory, 'diff.txt')):
            lines.append('    download the full diff:  %s/raw/diff?download=1' % page)
    return lines


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


GIT_DIFF_TIMEOUT = 600


def histogram_diff(expected_path, actual_path, labels, cwd):
    """Unified diff of two files made by `git diff --histogram`, with the same two header lines as
    `diff -u --label`. Returns None when git cannot make it, so the caller keeps the diff it already has."""
    env = {'PATH': os.environ.get('PATH', os.defpath), 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_PAGER': 'cat', 'HOME': os.path.expanduser('~')}
    expected_path = os.path.abspath(expected_path)
    actual_path   = os.path.abspath(actual_path)
    try:
        run = subprocess.run(
            ['git', 'diff', '--no-index', '--histogram', '--no-color', '--no-ext-diff', '--no-renames',
             '--text', '--no-prefix', '--unified=3', '--', expected_path, actual_path],
            cwd = cwd, env = env, stdout = subprocess.PIPE, stderr = subprocess.DEVNULL, timeout = GIT_DIFF_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode != 1:
        return None
    text  = run.stdout.decode('utf8', 'replace')
    start = 0 if text.startswith('@@') else text.find('\n@@') + 1
    if start == 0 and not text.startswith('@@'):
        return None
    body = re.sub(r'^(@@ [^@\n]*@@).*$', r'\1', text[start:], flags = re.M)
    return '--- %s\n+++ %s\n%s' % (labels[0], labels[1], body)


def analyze_failure(difftext, expected_path, markers, rule = None):
    """Returns (diff text, stats, summary). With a record rule for the output file, every hunk is cut at the
    record (document) boundaries of the expected file and labelled with its record; the label follows the
    closing @@ of each hunk in the returned text. Without a rule, labels come from the context markers."""
    from hub import diffparse

    head  = difftext[:difftext.find('\n@@') + 1] if '\n@@' in difftext else ''
    hunks = diffparse.parse_unified(difftext)
    starts = labels = None
    if rule and hunks:
        starts, labels = diffparse.record_index(expected_path, rule)
    if starts:
        hunks = diffparse.parse_unified(diffparse.split_at_documents(hunks, starts))
        diffparse.label_by_records(hunks, starts, labels)
        difftext = head + diffparse.format_unified(hunks)
    else:
        diffparse.label_hunks(hunks, expected_path, markers)
        difftext = head + diffparse.format_unified(hunks) if hunks else difftext
    stats = diffparse.summarize(hunks)
    summary = [{'old_start': h['old_start'], 'new_start': h['new_start'],
                'line': h['change_line'] or h['old_start'],
                'added': h['added'], 'removed': h['removed'], 'label': h['label']}
               for h in hunks[:MAX_HUNK_SUMMARY]]
    labelled = []
    for h in hunks:
        if h['label'] and h['label'] not in labelled:
            labelled.append(h['label'])
    stats['labels'] = len(labelled)
    stats['documents'] = len(diffparse.group_by_label(hunks))
    return difftext, stats, summary


def describe_diff(stats, summary):
    text = 'Output differs from expected: %d hunk%s, +%d / -%d lines, first difference at expected line %s' % (
        stats['hunks'], '' if stats['hunks'] == 1 else 's', stats['added'],
        stats['removed'], stats['first_line'])
    if summary and summary[0]['label']:
        text += ' (near: %s)' % summary[0]['label']
    if stats.get('labels', 0) > 1:
        text += '; %d distinct documents/sections affected' % stats['labels']
    return text


def execute_test(regression, entry, get_obj, tdir, markers, formats = ()):
    from hub import diffparse
    from hub.store import now, write_json

    os.makedirs(tdir, exist_ok = True)
    result = {'id': entry['id'], 'module': entry['module'], 'name': entry['name'],
              'output': entry['output'], 'status': 'error', 'reason': None,
              'started': now(), 'finished': None, 'duration': None,
              'error': None, 'diff': None, 'hunks': [], 'actual_bytes': None,
              'expected_bytes': None}
    started = time.time()
    scratch = tempfile.TemporaryDirectory(dir = tdir, prefix = '.scratch-')
    actual_tmp = os.path.join(scratch.name, 'actual')
    expected_path = os.path.join('expected', entry['output']) if entry['output'] else None

    handler = None
    try:
        with open(os.path.join(tdir, 'log.txt'), 'w', encoding = 'utf8', buffering = 1) as log:
            handler = logging.StreamHandler(log)
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
            logging.getLogger().addHandler(handler)
            out = Tee(sys.stdout, log)
            err = Tee(sys.stderr, log)
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    obj = get_obj(entry['module'])
                    if '__initialize__' in dir(obj):
                        obj.__initialize__()
                    method = getattr(obj, entry['name'])
                    if entry['output']:
                        with open(actual_tmp, 'w', encoding = 'utf8', newline = '') as outhandle:
                            method(outhandle)
                        status, difftext = regression.compare_files(
                            expected_path, actual_tmp,
                            labels = ('expected/%s' % entry['output'], 'actual/%s' % entry['output']))
                        result['status'] = status
                        if status == regression.FAILED:
                            git_diff = histogram_diff(expected_path, actual_tmp,
                                                      ('expected/%s' % entry['output'], 'actual/%s' % entry['output']),
                                                      tdir)
                            if git_diff is None:
                                print('note: git diff --histogram could not be used; diff.txt is the plain diff -u')
                            difftext, stats, summary = analyze_failure(
                                git_diff or difftext, expected_path, markers,
                                diffparse.rule_for(entry['output'], formats))
                            result['diff'] = stats
                            result['hunks'] = summary
                            result['reason'] = describe_diff(stats, summary)
                            with open(os.path.join(tdir, 'diff.txt'), 'w', encoding = 'utf8') as dh:
                                dh.write(difftext)
                        elif status == regression.NO_EXPECTED:
                            result['reason'] = 'No expected file at regressions/expected/%s; the test produced %s bytes of output' % (
                                entry['output'], file_size(actual_tmp))
                    else:
                        retval = method()
                        result['status'] = regression.PASSED if retval else regression.FAILED
                        if not retval:
                            result['reason'] = 'Test returned %r (falsy) instead of a truthy value' % (retval,)
            except Exception as exc:
                result['status'] = regression.ERROR
                result['error']  = exception_info(exc)
                result['reason'] = '%s: %s' % (type(exc).__name__, str(exc)[:300])
                log.write('\n' + result['error']['traceback'])
            finally:
                logging.getLogger().removeHandler(handler)
    finally:
        if result['status'] != 'passed' and os.path.exists(actual_tmp):
            shutil.move(actual_tmp, os.path.join(tdir, 'actual'))
        if result['status'] != 'passed' and expected_path and os.path.exists(expected_path):
            shutil.copyfile(expected_path, os.path.join(tdir, 'expected'))
        scratch.cleanup()

    result['actual_bytes']   = file_size(os.path.join(tdir, 'actual'))
    result['expected_bytes'] = file_size(os.path.join(tdir, 'expected'))
    result['finished'] = now()
    result['duration'] = round(time.time() - started, 2)
    write_json(os.path.join(tdir, 'result.json'), result)
    return result


def load_environment(args):
    import regression
    from indianlaw.setup.data_srcs import DataSrcFactory
    from indianlaw.utils import commonfuncs as utilscommon
    from indianlaw.storage_manager import StorageManager

    utilscommon.setup_logging(args.loglevel)
    storage  = StorageManager(args.profile)
    srcs     = DataSrcFactory(storage.get_datadir()).get_all_srcs(args.datasrc)
    return regression, storage, srcs


def cmd_sync(args):
    use_repo(args.repo_dir)
    from hub.store import Store

    with repo_lock():
        revision = fetch_branch(args.branch)
        store    = Store(args.results_dir)
        last     = store.last_tested(args.branch)
        action   = 'run'
        if args.trigger == 'push':
            if last and last.get('revision') == revision:
                action = 'skip'
            else:
                store.set_last_tested(args.branch, revision, None)
    print('BRANCH: %s' % args.branch)
    print('REVISION: %s' % revision)
    print('ACTION: %s' % action)
    if action == 'skip':
        print('No new commits on %s since the last scheduled run (%s). Nothing to run.' % (args.branch, revision[:10]))
    else:
        print('git fetch origin done. Latest commit of %s is %s.' % (args.branch, revision[:10]))
    return 0


def cmd_checkout(args):
    use_repo(args.repo_dir)
    dest = add_worktree(args.dest, args.revision)
    print('checked out %s at %s' % (args.revision[:10], dest))
    return 0


def cmd_sweep(args):
    use_repo(args.repo_dir)
    base = expand_path(args.base)
    if args.all:
        busy = set()
    else:
        busy = buildbot_in_use(args.buildbot_url)
        if busy is None:
            print('Cannot ask Buildbot which builds are running (%s); no checkout removed' % args.buildbot_url)
            return 0
    removed = sweep_trees(base, busy, dry_run = args.dry_run)
    verb = 'would remove' if args.dry_run else 'removed'
    for path in removed:
        print('%s old checkout %s' % (verb, path))
    print('%d old checkout%s %s' % (len(removed), '' if len(removed) == 1 else 's', 'to remove' if args.dry_run else 'removed'))
    return 0


def cmd_prune(args):
    from hub.store import Store
    store = Store(args.results_dir)
    names, size = store.prune(args.days, keep_latest = args.keep, dry_run = not args.yes)
    for name in names:
        print('%s %s' % ('removed' if args.yes else 'would remove', name))
    print('%d run%s %s, %.1f MB' % (len(names), '' if len(names) == 1 else 's', 'removed' if args.yes else 'to remove', size / 1048576))
    if not args.yes and names:
        print('Nothing was deleted. Add --yes to delete them.')
    return 0


def cmd_release(args):
    use_repo(args.repo_dir)
    remove_worktree(args.dest)
    print('released %s' % os.path.abspath(args.dest))
    return 0


def cmd_catalog(args):
    from hub.store import Store
    if not args.branch:
        ensure_paths(args.repo_dir)
        import regression
        try:
            catalog = build_catalog(regression)
        except RuntimeError as exc:
            raise SystemExit(str(exc))
        branch  = current_branch()
        Store(args.results_dir).save_catalog(catalog, branch)
        print('catalog: %d modules, %d tests for %s @ %s' % (len(catalog['modules']), len(catalog['tests']),
                                                            branch, (catalog['revision'] or '')[:10]))
        return 0

    store = Store(args.results_dir)
    try:
        catalog, revision = read_branch_catalog(args)
    except SystemExit as exc:
        store.save_catalog_error(args.branch, str(exc.code))
        raise
    except Exception as exc:
        store.save_catalog_error(args.branch, '%s: %s' % (type(exc).__name__, exc))
        raise
    store.save_catalog(catalog, args.branch)
    print('catalog: %d modules, %d tests for %s @ %s' % (len(catalog['modules']), len(catalog['tests']),
                                                        args.branch, revision[:10]))
    return 0


def read_branch_catalog(args):
    global REPO
    use_repo(args.repo_dir)
    source = REPO
    with repo_lock():
        revision = fetch_branch(args.branch)
    scratch = tempfile.mkdtemp(prefix = 'ci-catalog-')
    try:
        tree = add_worktree(os.path.join(scratch, 'indianlaw'), revision)
        try:
            ensure_paths(tree)
            import regression
            try:
                catalog = build_catalog(regression)
            except RuntimeError as exc:
                raise SystemExit('%s: %s' % (args.branch, exc))
        finally:
            os.chdir(HERE)
            REPO = source
            remove_worktree(tree)
    finally:
        shutil.rmtree(scratch, ignore_errors = True)
    return catalog, revision


def cmd_profile(args):
    use_repo(args.repo_dir)
    from hub import profile
    args.file = expand_path(args.file)
    if not os.path.isfile(args.file):
        raise SystemExit('Profile file not found: %s' % args.file)
    target = os.path.join(REPO, '.env.%s' % args.profile)
    existing = ''
    if os.path.exists(target):
        with open(target, 'r', encoding = 'utf8') as handle:
            existing = handle.read()
    with open(args.file, 'r', encoding = 'utf8') as handle:
        merged, applied = profile.merge(existing, handle.read())
    with open(target, 'w', encoding = 'utf8') as handle:
        handle.write(merged)
    print('profile: %d setting(s) from %s applied to .env.%s%s' % (
        len(applied), args.file, args.profile, (': ' + ', '.join(applied)) if applied else ' (nothing needed changing)'))
    return 0


def cmd_select(args):
    ensure_paths(args.repo_dir)
    import regression
    from hub.store import Store

    store   = Store(args.results_dir)
    catalog = build_catalog(regression)
    plan    = make_plan(args, catalog, store)
    print('mode: %s  base: %s  changed files: %d' % (plan['mode'], plan['base'], len(plan['changed_files'])))
    for path in plan['unmapped']:
        print('unmapped: %s' % path)
    if plan['unmatched_specs']:
        print('no such test: %s' % ' '.join(plan['unmatched_specs']))
        return 2
    for test_id in plan['tests']:
        print('%s    %s' % (test_id, '; '.join(plan['reasons'].get(test_id, [])[:2])))
    print('%d tests selected' % len(plan['tests']))
    return 0


def cmd_run(args):
    ensure_paths(args.repo_dir)
    from hub import selection
    from hub.diffparse import compile_formats, compile_markers
    from hub.store import Store, now

    store    = Store(args.results_dir)
    run_id   = args.run_id or store.new_run_id('b%s' % args.build_number if args.build_number else 'cli')
    run_dir  = store.run_path(run_id)
    os.makedirs(run_dir, exist_ok = True)

    console = open(os.path.join(run_dir, 'runner.log'), 'a', encoding = 'utf8', buffering = 1)
    sys.stdout = Tee(sys.__stdout__, console)
    sys.stderr = Tee(sys.__stderr__, console)
    signal.signal(signal.SIGTERM, on_sigterm)

    previous = store.get_run(run_id) or {}
    revision = selection.head_revision(REPO)
    branch   = args.branch or current_branch()
    run = initial_run(args, run_id, previous, branch, revision)
    store.save_run(run_id, run)

    ui = os.environ.get('REG_UI_URL')
    print('run %s on %s @ %s' % (run_id, branch, (revision or '')[:10]))
    if ui:
        print('details: %s/run/%s' % (ui.rstrip('/'), run_id))

    exit_code = 1
    try:
        regression, storage, srcs = load_environment(args)
        catalog = build_catalog(regression)
        store.save_catalog(catalog, branch)

        plan = make_plan(args, catalog, store)
        run['selection'] = selection_record(plan)

        if plan['unmatched_specs']:
            run['state']   = 'finished'
            run['result']  = 'error'
            run['message'] = 'No such test or module: %s' % ' '.join(plan['unmatched_specs'])
            run['finished'] = now()
            store.save_run(run_id, run)
            print(run['message'])
            return 2

        by_id = {t['id']: t for t in catalog['tests']}
        run['tests'] = [{'id': i, 'status': PENDING, 'duration': None} for i in plan['tests']]
        run['counts'] = count_statuses(run['tests'])
        store.save_run(run_id, run)
        print('%d tests selected (%s)' % (len(plan['tests']), plan['mode']))

        get_obj = make_loader(regression, storage, srcs)

        impact  = selection.load_impact_map(os.path.join(HERE, 'hub', 'impact_map.json'))
        markers = compile_markers(impact.get('context_markers', []))
        formats = compile_formats(impact.get('record_formats', []))

        total = len(run['tests'])
        for index, item in enumerate(run['tests'], 1):
            run['current'] = item['id']
            item['status'] = 'running'
            store.save_run(run_id, run)
            print('[%d/%d] %s' % (index, total, item['id']))

            result = execute_test(regression, by_id[item['id']], get_obj,
                                  store.test_path(run_id, item['id']), markers, formats)
            item['status']   = result['status']
            item['duration'] = result['duration']
            run['counts'] = count_statuses(run['tests'])
            store.save_run(run_id, run)
            print('    -> %s (%.1fs)%s' % (result['status'], result['duration'],
                                          ' - ' + result['reason'] if result['reason'] else ''))
            for line in saved_files_lines(store.test_path(run_id, item['id']), run_id, item['id'], result['status']):
                print(line)

        bad = [i for i in run['tests'] if i['status'] != 'passed']
        run['result'] = 'failed' if bad else 'passed'
        run['state']  = 'finished'
        exit_code = 1 if bad else 0
        if not run['tests']:
            run['message'] = 'No regression tests are impacted by this change'
    except Terminated:
        run['state']   = 'aborted'
        run['result']  = 'aborted'
        run['message'] = 'Run was terminated (Buildbot cancel or timeout)'
        for item in run['tests']:
            if item['status'] in ('running', PENDING):
                item['status'] = ABORTED if item['status'] == 'running' else PENDING
        run['counts'] = count_statuses(run['tests'])
        exit_code = 1
    except Exception as exc:
        run['state']   = 'finished'
        run['result']  = 'error'
        run['message'] = '%s: %s' % (type(exc).__name__, exc)
        traceback.print_exc()
        exit_code = 1
    finally:
        run['current']  = None
        run['finished'] = now()
        store.save_run(run_id, run)

    if run['state'] == 'finished' and run['result'] in ('passed', 'failed') \
            and run['trigger'] == 'push' and revision:
        store.set_last_tested(branch, revision, run_id)

    counts = run['counts']
    print('summary: %d total, %d passed, %d failed, %d error, %d no_expected' % (
        counts['total'], counts['passed'], counts['failed'], counts['error'], counts['no_expected']))
    return exit_code


def cmd_plan(args):
    ensure_paths(args.repo_dir)
    from hub import selection
    from hub.store import Store, now

    store    = Store(args.results_dir)
    run_id   = args.run_id or store.new_run_id('b%s' % args.build_number if args.build_number else 'cli')
    os.makedirs(store.run_path(run_id), exist_ok = True)
    previous = store.get_run(run_id) or {}
    revision = selection.head_revision(REPO)
    branch   = args.branch or current_branch()
    run      = initial_run(args, run_id, previous, branch, revision)
    store.save_run(run_id, run)
    if args.trigger == 'push' and revision:
        store.set_last_tested(branch, revision, run_id)
    print('RUN_ID: %s' % run_id)

    try:
        import regression
        catalog = build_catalog(regression)
        store.save_catalog(catalog, branch)
        plan = make_plan(args, catalog, store)
        run['selection'] = selection_record(plan)

        if plan['unmatched_specs']:
            run['state']    = 'finished'
            run['result']   = 'error'
            run['message']  = 'No such test or module: %s' % ' '.join(plan['unmatched_specs'])
            run['finished'] = now()
            store.save_run(run_id, run)
            print(run['message'])
            return 2

        by_id   = {t['id']: t for t in catalog['tests']}
        modules = []
        for test_id in plan['tests']:
            if by_id[test_id]['module'] not in modules:
                modules.append(by_id[test_id]['module'])
        run['tests']  = [{'id': i, 'status': PENDING, 'duration': None} for i in plan['tests']]
        run['counts'] = count_statuses(run['tests'])
        run['shards'] = assign_shards(modules, args.max_shards)
        store.save_run(run_id, run)

        print('%d tests selected (%s)' % (len(plan['tests']), plan['mode']))
        for shard in run['shards']:
            print('shard %d: %s' % (shard['index'], ' '.join(shard['modules'])))
        print('SHARDS: %d' % len(run['shards']))
        return 0
    except Exception as exc:
        run['state']    = 'finished'
        run['result']   = 'error'
        run['message']  = '%s: %s' % (type(exc).__name__, exc)
        run['finished'] = now()
        store.save_run(run_id, run)
        traceback.print_exc()
        return 1


def cmd_shard(args):
    ensure_paths(args.repo_dir)
    from hub import selection
    from hub.diffparse import compile_formats, compile_markers
    from hub.store import Store, now

    store = Store(args.results_dir)
    run   = store.get_run(args.run_id) if args.run_id else None
    shard = next((s for s in (run or {}).get('shards', []) if s['index'] == args.shard_index), None)
    if not shard:
        print('run %s has no shard %s' % (args.run_id, args.shard_index))
        return 2

    console = open(os.path.join(store.run_path(args.run_id), 'runner.log'), 'a', encoding = 'utf8', buffering = 1)
    prefix  = '[shard %d] ' % shard['index']
    sys.stdout = Tee(sys.__stdout__, Prefixed(console, prefix))
    sys.stderr = Tee(sys.__stderr__, Prefixed(console, prefix))
    signal.signal(signal.SIGTERM, on_sigterm)

    data = {'index': shard['index'], 'modules': shard['modules'], 'state': 'running',
            'worker': args.worker or None,
            'build': {'number': args.build_number, 'url': args.build_url},
            'started': now(), 'finished': None, 'current': None, 'message': None, 'tests': []}
    store.save_shard(args.run_id, shard['index'], data)
    print('shard %d of run %s on %s: %s' % (shard['index'], args.run_id, args.worker or 'this worker',
                                           ' '.join(shard['modules'])))

    exit_code = 1
    try:
        env_error = None
        try:
            regression, storage, srcs = load_environment(args)
        except Exception as exc:
            import regression
            storage, srcs, env_error = None, [], exc
            traceback.print_exc()

        catalog = build_catalog(regression)
        by_id   = {t['id']: t for t in catalog['tests']}
        ids     = [t['id'] for t in run['tests']
                   if t['id'] in by_id and by_id[t['id']]['module'] in shard['modules']]
        data['tests'] = [{'id': i, 'status': PENDING, 'duration': None} for i in ids]
        store.save_shard(args.run_id, shard['index'], data)

        get_obj = make_loader(regression, storage, srcs, env_error)
        impact  = selection.load_impact_map(os.path.join(HERE, 'hub', 'impact_map.json'))
        markers = compile_markers(impact.get('context_markers', []))
        formats = compile_formats(impact.get('record_formats', []))

        total = len(data['tests'])
        for index, item in enumerate(data['tests'], 1):
            data['current'] = item['id']
            item['status']  = 'running'
            store.save_shard(args.run_id, shard['index'], data)
            print('[%d/%d] %s' % (index, total, item['id']))

            result = execute_test(regression, by_id[item['id']], get_obj,
                                  store.test_path(args.run_id, item['id']), markers, formats)
            item['status']   = result['status']
            item['duration'] = result['duration']
            store.save_shard(args.run_id, shard['index'], data)
            print('    -> %s (%.1fs)%s' % (result['status'], result['duration'],
                                          ' - ' + result['reason'] if result['reason'] else ''))
            for line in saved_files_lines(store.test_path(args.run_id, item['id']), args.run_id, item['id'], result['status']):
                print(line)

        bad = [i for i in data['tests'] if i['status'] != 'passed']
        if env_error:
            data['state']   = 'error'
            data['message'] = 'Environment failed to load: %s: %s' % (type(env_error).__name__, env_error)
        else:
            data['state'] = 'finished'
        exit_code = 1 if bad or env_error else 0
    except Terminated:
        data['state']   = 'aborted'
        data['message'] = 'Shard was terminated (Buildbot cancel or timeout)'
        for item in data['tests']:
            if item['status'] == 'running':
                item['status'] = ABORTED
    except Exception as exc:
        data['state']   = 'error'
        data['message'] = '%s: %s' % (type(exc).__name__, exc)
        traceback.print_exc()
    finally:
        data['current']  = None
        data['finished'] = now()
        store.save_shard(args.run_id, shard['index'], data)

    counts = count_statuses(data['tests'])
    print('summary: %d total, %d passed, %d failed, %d error, %d no_expected' % (
        counts['total'], counts['passed'], counts['failed'], counts['error'], counts['no_expected']))
    return exit_code


def cmd_finalize(args):
    ensure_paths(args.repo_dir)
    from hub.store import Store, now

    store = Store(args.results_dir)
    run   = store.get_run(args.run_id) if args.run_id else None
    if not run:
        print('no such run: %s' % args.run_id)
        return 2
    stop = store.get_stop(args.run_id)
    if run.get('state') == 'queued':
        run['state']    = 'finished'
        run['result']   = 'error'
        run['message']  = 'The build failed before any test was selected; see the Buildbot build log'
        if stop:
            run['state']   = 'aborted'
            run['result']  = 'aborted'
            run['message'] = 'Stopped by %s before any test ran' % stop.get('by')
            for item in run.get('tests', []):
                item['status'] = ABORTED
            run['counts'] = count_statuses(run.get('tests', []))
        run['finished'] = now()
        store.save_run(args.run_id, run)
        print(run['message'])
        return 1
    if run.get('state') != 'running':
        print('run already %s: %s' % (run.get('state'), run.get('result')))
        return 0 if run.get('result') == 'passed' else 1

    shard_of   = {m: s for s in run.get('shards', []) for m in s['modules']}
    incomplete = [s for s in run.get('shards', []) if s.get('state') != 'finished']
    for item in run['tests']:
        if item['status'] in (PENDING, 'running'):
            shard = shard_of.get(item['id'].split('.', 1)[0]) or {}
            item['status'] = 'error' if shard.get('state') == 'error' else ABORTED

    counts = count_statuses(run['tests'])
    bad    = [i for i in run['tests'] if i['status'] != 'passed']
    if any(s.get('state') == 'error' for s in incomplete):
        run['result'] = 'error'
    elif incomplete:
        run['result'] = 'aborted'
    else:
        run['result'] = 'failed' if bad else 'passed'
    run['state'] = 'aborted' if run['result'] == 'aborted' else 'finished'

    messages = ['Shard %d (%s) did not finish: %s%s' % (
        s['index'], ' '.join(s['modules']), s.get('state') or 'never started',
        ' - ' + s['message'] if s.get('message') else '') for s in incomplete]
    if stop and run['result'] == 'aborted':
        run['message'] = 'Stopped by %s' % stop.get('by')
    elif messages:
        run['message'] = '; '.join(messages)
    elif not run['tests']:
        run['message'] = 'No regression tests are impacted by this change'
    run['counts']   = counts
    run['current']  = None
    run['finished'] = now()
    store.save_run(args.run_id, run)

    print('summary: %d total, %d passed, %d failed, %d error, %d no_expected, %d aborted' % (
        counts['total'], counts['passed'], counts['failed'], counts['error'],
        counts['no_expected'], counts['aborted']))
    return 0 if run['result'] == 'passed' else 1


def get_parser():
    parser = argparse.ArgumentParser(description = 'Structured regression runner')
    parser.add_argument('--repo-dir', default = None,
                        help = 'checkout of the code under test, named indianlaw (default: REG_REPO_DIR, else ../indianlaw)')
    parser.add_argument('--results-dir', default = os.environ.get('REG_RESULTS_DIR'),
                        required = 'REG_RESULTS_DIR' not in os.environ, help = 'default: REG_RESULTS_DIR')
    sub = parser.add_subparsers(dest = 'command', required = True)

    def common(p):
        p.add_argument('--tests', default = '', help = 'modules, test ids or globs, space/comma separated')
        p.add_argument('--all', action = 'store_true', help = 'run every test')
        p.add_argument('--base', default = None, help = 'diff base revision for impact selection')
        p.add_argument('--branch', default = None)

    run = sub.add_parser('run')
    common(run)
    run.add_argument('--profile', default = os.environ.get('REGRESSION_PROFILE'), required = 'REGRESSION_PROFILE' not in os.environ)
    run.add_argument('--datasrc', nargs = '*', default = [])
    run.add_argument('--loglevel', default = 'info')
    run.add_argument('--run-id', default = '')
    run.add_argument('--trigger', default = '')
    run.add_argument('--requested-by', default = '')
    run.add_argument('--reason', default = '')
    run.add_argument('--build-number', default = '')
    run.add_argument('--build-url', default = '')
    run.set_defaults(func = cmd_run)

    plan = sub.add_parser('plan')
    common(plan)
    plan.add_argument('--profile', default = os.environ.get('REGRESSION_PROFILE'), required = 'REGRESSION_PROFILE' not in os.environ)
    plan.add_argument('--run-id', default = '')
    plan.add_argument('--trigger', default = '')
    plan.add_argument('--requested-by', default = '')
    plan.add_argument('--reason', default = '')
    plan.add_argument('--build-number', default = '')
    plan.add_argument('--build-url', default = '')
    plan.add_argument('--max-shards', type = int, default = 32)
    plan.set_defaults(func = cmd_plan)

    shard = sub.add_parser('shard')
    shard.add_argument('--run-id', required = True)
    shard.add_argument('--shard-index', type = int, required = True)
    shard.add_argument('--profile', default = os.environ.get('REGRESSION_PROFILE'), required = 'REGRESSION_PROFILE' not in os.environ)
    shard.add_argument('--datasrc', nargs = '*', default = [])
    shard.add_argument('--loglevel', default = 'info')
    shard.add_argument('--worker', default = '')
    shard.add_argument('--build-number', default = '')
    shard.add_argument('--build-url', default = '')
    shard.set_defaults(func = cmd_shard)

    finalize = sub.add_parser('finalize')
    finalize.add_argument('--run-id', default = '')
    finalize.set_defaults(func = cmd_finalize)

    select = sub.add_parser('select')
    common(select)
    select.set_defaults(func = cmd_select)

    catalog = sub.add_parser('catalog')
    catalog.add_argument('--branch', default = None)
    catalog.set_defaults(func = cmd_catalog)

    sync = sub.add_parser('sync')
    sync.add_argument('--branch', required = True)
    sync.add_argument('--trigger', default = '')
    sync.set_defaults(func = cmd_sync)

    checkout = sub.add_parser('checkout')
    checkout.add_argument('--dest', required = True)
    checkout.add_argument('--revision', required = True)
    checkout.set_defaults(func = cmd_checkout)

    profile = sub.add_parser('profile')
    profile.add_argument('--file', required = True)
    profile.add_argument('--profile', required = True)
    profile.set_defaults(func = cmd_profile)

    sweep = sub.add_parser('sweep')
    sweep.add_argument('--base', default = os.environ.get('REG_WORKERS_DIR'), required = 'REG_WORKERS_DIR' not in os.environ,
                       help = 'folder that holds the worker folders (default: REG_WORKERS_DIR)')
    sweep.add_argument('--buildbot-url', default = os.environ.get('REG_BUILDBOT_URL'),
                       required = 'REG_BUILDBOT_URL' not in os.environ, help = 'default: REG_BUILDBOT_URL')
    sweep.add_argument('--all', action = 'store_true',
                       help = 'remove every checkout; only for when Buildbot is stopped')
    sweep.add_argument('--dry-run', action = 'store_true', help = 'only list what would be removed')
    sweep.set_defaults(func = cmd_sweep)

    prune = sub.add_parser('prune')
    prune.add_argument('--days', type = int, required = True, help = 'remove runs that ended more than this many days ago')
    prune.add_argument('--keep', type = int, default = 50, help = 'never remove the newest N runs (default 50)')
    prune.add_argument('--yes', action = 'store_true', help = 'really delete; without it only lists')
    prune.set_defaults(func = cmd_prune)

    release = sub.add_parser('release')
    release.add_argument('--dest', required = True)
    release.set_defaults(func = cmd_release)
    return parser


if __name__ == '__main__':
    arguments = get_parser().parse_args()
    sys.exit(arguments.func(arguments))
