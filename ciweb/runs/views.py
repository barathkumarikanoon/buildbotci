import os

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from ciweb.repos import health, publish
from ciweb.repos.models import RepoProfile
from ciweb.runs import activity, buildbot_client, fmt
from hub import diffparse, selection
from hub.store import FAILING_STATUS, RUNNING_STATES, Store, abort_unfinished, now, valid_id

STATUS_ORDER = {'error': 0, 'failed': 1, 'no_expected': 2, 'aborted': 3,
                'running': 4, 'pending': 5, 'passed': 6}
RAW_FILES    = {'actual': 'actual', 'expected': 'expected', 'diff': 'diff.txt', 'log': 'log.txt'}
INLINE_LIMIT = 256 * 1024
TAIL_LIMIT   = 128 * 1024
DOCS_PER_PAGE  = 15
MAX_SUMMARY_ROWS = 1000
SIGNS = {'add': '+', 'del': '-', 'ctx': ' '}


def get_repo(key):
    """The repo profile of the page. A disabled one can still be read, but not run."""
    try:
        return RepoProfile.objects.get(key = key)
    except RepoProfile.DoesNotExist:
        raise Http404()


def store(repo):
    return Store(repo.to_config().results_dir(settings.RESULTS_DIR))


def load_run(repo, run_id):
    if not valid_id(run_id):
        raise Http404()
    run = store(repo).get_run(run_id)
    if not run:
        raise Http404()
    return run


def number(request, name = 'page'):
    try:
        return max(int(request.GET.get(name, 1) or 1), 1)
    except ValueError:
        return 1


def tail_file(path, offset):
    try:
        size_now = os.path.getsize(path)
    except OSError:
        return '', 0
    if offset > size_now:
        offset = 0
    if size_now - offset > TAIL_LIMIT:
        offset = size_now - TAIL_LIMIT
    with open(path, 'rb') as handle:
        handle.seek(offset)
        data = handle.read(TAIL_LIMIT)
    return data.decode('utf8', 'replace'), offset + len(data)


def bar_segments(counts):
    counts = counts or {}
    total = counts.get('total') or 0
    if not total:
        return []
    return [{'key': key, 'width': counts[key] * 100.0 / total}
            for key in ('passed', 'failed', 'error', 'no_expected', 'aborted', 'running') if counts.get(key)]


def clock_text(hour, minute):
    """The time of a nightly run for people: 21:00, or the cron text when it is not one time."""
    if str(hour).isdigit() and str(minute).isdigit():
        return '%02d:%02d' % (int(hour), int(minute))
    return 'hour %s, minute %s' % (hour, minute)


def options(values, chosen):
    return [{'value': v, 'selected': v == chosen} for v in values]


def run_summary(repo, run):
    counts = run.get('counts') or {}
    return dict(run, url = reverse('run_view', args = [repo.key, run['id']]), segments = bar_segments(counts),
                done = (counts.get('total') or 0) - (counts.get('pending') or 0) - (counts.get('running') or 0))


def failing_count(run):
    counts = (run or {}).get('counts') or {}
    return (counts.get('failed') or 0) + (counts.get('error') or 0) + (counts.get('no_expected') or 0)


HEALTH_LABEL = {'bad': 'Failing', 'warn': 'Stuck', 'run': 'Running', 'ok': 'Passing', 'idle': 'No runs yet'}
HEALTH_ORDER = ('bad', 'warn', 'run', 'ok', 'idle')
HISTORY_LENGTH = 12


def overview_hero(level, items, cards):
    """The headline of the start page: one sentence that says how things are, in the colour of the worst thing."""
    failing = sum(1 for item in items if item['level'] == 'bad')
    if level == 'bad':
        title = '%d branch%s failing' % (failing, ' is' if failing == 1 else 'es are')
        text  = 'The latest run of %s did not pass. Open it to see which tests and why.' % ('this branch' if failing == 1 else 'each of them')
    elif level == 'warn':
        title, text = 'Some runs are stuck', 'They will not finish by themselves. Open the repository to see why.'
    elif level == 'run':
        title, text = 'Tests are running', 'Nothing has failed. The results appear here as each run ends.'
    elif level == 'ok':
        title, text = 'Everything is passing', 'The latest run of every tested branch passed.'
    else:
        title, text = 'Nothing has been tested yet', 'Open a repository and press Run tests, or wait for its nightly time.'
    return {'level': level, 'title': title, 'text': text}


@require_safe
def index(request):
    """The start page: how things are (one headline), what is wrong, and every enabled repository with the history of each branch."""
    cards, attention = [], []
    live = activity.buildbot_live()
    for repo in RepoProfile.objects.filter(enabled = True).prefetch_related('branches'):
        data   = store(repo)
        config = repo.to_config()
        recent = activity.settle(data.list_runs(limit = 80)[0], live)                      # newest first
        busy   = activity.settle(data.list_runs(limit = 20, state = 'running', recent = 100)[0] + data.list_runs(limit = 20, state = 'queued', recent = 100)[0], live)
        here   = reverse('repo_runs', args = [repo.key])
        lines  = []
        for branch in repo.branch_list():
            runs  = [r for r in recent if r.get('branch') == branch.name]
            run   = runs[0] if runs else None
            badge = (run or {}).get('result') or (run or {}).get('state') or ''
            bad   = failing_count(run) if badge in ('failed', 'error', 'no_expected') else 0
            lines.append({'name': branch.name, 'badge': badge, 'bad': bad, 'rev': ((run or {}).get('revision') or '')[:8],
                          'ago': fmt.ago((run or {}).get('started') or (run or {}).get('created')) if run else '',
                          'history': [{'badge': r.get('result') or r.get('state') or 'pending',
                                       'title': '%s, %s' % (fmt.label(r.get('result') or r.get('state') or 'pending'), fmt.ago(r.get('started') or r.get('created')))}
                                      for r in reversed(runs[:HISTORY_LENGTH])]})
            if bad:
                attention.append({'level': 'bad', 'url': reverse('run_view', args = [repo.key, run['id']]),
                                  'title': '%s, branch %s' % (repo.name, branch.name),
                                  'text': '%d test%s failing, %s' % (bad, '' if bad == 1 else 's', fmt.ago(run.get('started') or run.get('created')))})
        progress = activity.summary(busy)
        for kind, one, many in (('waiting', '1 run waits for a worker', '%d runs wait for a worker'),
                                ('stalled', '1 run stopped without an end', '%d runs stopped without an end')):
            if progress[kind]:
                attention.append({'level': 'warn', 'url': here, 'title': repo.name, 'text': one if progress[kind] == 1 else many % progress[kind]})
        badges = [line['badge'] for line in lines]
        level  = ('bad' if any(line['bad'] for line in lines) else 'warn' if progress['waiting'] or progress['stalled']
                  else 'run' if progress['running'] or 'running' in badges else 'ok' if 'passed' in badges else 'idle')
        cards.append({'repo': repo, 'lines': lines, 'activity': progress, 'url': here, 'level': level, 'health': HEALTH_LABEL[level],
                      'new_url': reverse('new_run', args = [repo.key]),
                      'nightly': ('Nightly at %s' % clock_text(config.cron_hour, config.cron_minute)
                                  if config.schedule and config.scheduled_branches else 'Tested on request only')})
    worst = min((c['level'] for c in cards), key = HEALTH_ORDER.index, default = 'idle')
    return render(request, 'runs/home.html', {'cards': cards, 'attention': attention[:4], 'hero': overview_hero(worst, attention, cards)})


def connected_workers():
    """The workers the master reports, {name: {'connected', 'busy'}}, or None when Buildbot cannot be asked (a short wait: a page
    must not hang because the master is down)."""
    try:
        return buildbot_client.worker_states(settings.BUILDBOT_URL, settings.BUILDBOT_USER, settings.BUILDBOT_PASS, timeout = 3)
    except buildbot_client.BuildbotError:
        return None


def repo_status(repo, config, busy, staff):
    """The strip under the title of a repository page: can a run start, and when is the next nightly one? Plus an alert, with the
    way out, when nothing can run. Returns (items, alert or None)."""
    states = connected_workers()
    rows = health.worker_rows(config, settings.WORKERS_DIR, states)
    up = sum(1 for r in rows if r['state'] in (health.CONNECTED, health.BUSY))
    sync, sync_text = publish.sync_state()
    waiting = sum(1 for r in busy if r.get('state') in ('queued', 'waiting'))
    if states is None:
        workers = {'label': 'Workers', 'value': 'Unknown', 'level': 'idle', 'note': 'Buildbot could not be asked'}
    else:
        workers = {'label': 'Workers', 'value': '%d of %d connected' % (up, len(rows)), 'level': 'bad' if not up else 'warn' if up < len(rows) else 'ok',
                   'note': 'Each module of a run goes to its own worker'}
    nightly = ({'label': 'Nightly runs', 'value': 'At %s' % clock_text(config.cron_hour, config.cron_minute), 'level': 'ok', 'note': 'when a branch has new commits'}
               if config.schedule and config.scheduled_branches else {'label': 'Nightly runs', 'value': 'Off', 'level': 'idle', 'note': 'runs start on request only'})
    alert = None
    if not repo.enabled:
        alert = {'level': 'warn', 'title': 'This repository is disabled', 'text': 'Nothing is tested and nobody can start a run until an administrator enables it.'}
    elif states is None:
        alert = {'level': 'bad', 'title': 'Buildbot cannot be reached', 'text': 'Runs cannot start until the Buildbot master is running. An administrator can check it with bin/master status.'}
    elif not up:
        alert = {'level': 'bad', 'title': 'No worker is connected',
                 'text': ('%d run%s waiting for one. ' % (waiting, ' is' if waiting == 1 else 's are') if waiting else 'A run you start now would wait until one connects. ') +
                         ('Create and start the workers to continue.' if staff else 'Ask an administrator to start the workers.'),
                 'action': staff}
    elif sync == 'error':
        alert = {'level': 'bad', 'title': 'Buildbot could not load this repository\'s settings', 'text': sync_text}
    return [workers, nightly], alert


@require_safe
def repo_runs(request, key):
    """One repo: its branches (click one to see its results) and its runs."""
    repo   = get_repo(key)
    config = repo.to_config()
    branch = request.GET.get('branch') or None
    result = request.GET.get('result') or None
    by     = request.GET.get('by') or None
    page   = number(request)
    per    = 30
    data   = store(repo)
    runs, total = data.list_runs(limit = per, offset = (page - 1) * per, branch = branch, result = result, requested_by = by)
    live = activity.buildbot_live()
    runs = activity.settle(runs, live)
    names  = sorted({r.get('requested_by') for r in data.list_runs(limit = 200)[0] if r.get('requested_by')},
                    key = lambda name: (name != settings.AUTO_NAME, name.lower()))
    rows = []
    for run in runs:
        counts = run.get('counts') or {}
        rows.append(dict(run, url = reverse('run_view', args = [key, run['id']]),
                         short_rev = (run.get('revision') or '')[:8],
                         duration = fmt.span(fmt.run_duration(run)),
                         started_text = fmt.clock(run.get('started') or run.get('created')),
                         bad = (counts.get('failed') or 0) + (counts.get('error') or 0) + (counts.get('no_expected') or 0),
                         segments = bar_segments(counts),
                         badge = run.get('result') or run.get('state')))
    busy = [run_summary(repo, run) for run in activity.settle(data.list_runs(limit = 10, state = 'running', recent = 300)[0]
                                                               + data.list_runs(limit = 10, state = 'queued', recent = 300)[0], live)]
    latest = {name: activity.settle([run], live)[0] for name, run in data.latest_by_branch(config.branch_names).items()}
    last_scheduled = data.all_last_tested()
    tabs = []
    for name in config.branch_names:
        run  = latest.get(name)
        info = last_scheduled.get(name) or {}
        tabs.append({'name': name, 'current': name == branch,
                     'badge': (run or {}).get('result') or (run or {}).get('state') or '',
                     'ago': fmt.ago((run or {}).get('started') or (run or {}).get('created')) if run else 'no runs yet',
                     'short': ((run or {}).get('revision') or '')[:8],
                     'scheduled': name in config.scheduled_branches,
                     'when': ('Nightly at %s' % clock_text(*config.branch_cron(name))) if name in config.scheduled_branches else 'On request only',
                     'new_url': fmt.with_query(reverse('new_run', args = [key]), branch = name),
                     'url': fmt.with_query(reverse('repo_runs', args = [key]), branch = name),
                     'run_url': reverse('run_view', args = [key, run['id']]) if run else ''})
    params = {'branch': branch, 'result': result, 'by': by}
    here = reverse('repo_runs', args = [key])
    status_items, alert = repo_status(repo, config, busy, request.user.is_staff)
    return render(request, 'runs/repo_runs.html', {
        'status_items': status_items, 'alert': alert,
        'default_time': clock_text(config.cron_hour, config.cron_minute), 'nightly_on': config.schedule,
        'repo': repo, 'rows': rows, 'total': total, 'plural': '' if total == 1 else 's', 'busy': busy, 'busy_text': activity.summary(busy)['text'], 'tabs': tabs,
        'branch': branch, 'new_url': fmt.with_query(reverse('new_run', args = [key]), branch = branch) if branch else reverse('new_run', args = [key]),
        'branch_options': options(config.branch_names, branch),
        'result_options': options(('passed', 'failed', 'error', 'aborted'), result),
        'requester_options': options(names, by),
        'newer': fmt.with_query(here, page = page - 1, **params) if page > 1 else '',
        'older': fmt.with_query(here, page = page + 1, **params) if page * per < total else '',
    })


@require_safe
def legacy_run(request, run_id):
    """Addresses from before the CI had several repos (/run/<id>) lead to the repo that has the run."""
    if valid_id(run_id):
        for repo in RepoProfile.objects.all():
            if store(repo).get_run(run_id):
                return redirect('run_view', key = repo.key, run_id = run_id)
    raise Http404()


@require_safe
def run_view(request, key, run_id):
    repo   = get_repo(key)
    run    = load_run(repo, run_id)
    status = request.GET.get('status') or ''
    query  = (request.GET.get('q') or '').strip().lower()
    tests  = list(run.get('tests', []))
    if status == 'failing':
        tests = [t for t in tests if t['status'] in FAILING_STATUS]
    elif status:
        tests = [t for t in tests if t['status'] == status]
    if query:
        tests = [t for t in tests if query in t['id'].lower()]
    tests.sort(key = lambda t: (STATUS_ORDER.get(t['status'], 9), t['id']))
    data = store(repo)
    rows = []
    for item in tests:
        reason = ''
        if item['status'] in FAILING_STATUS:
            reason = (data.get_result(run_id, item['id']) or {}).get('reason') or ''
        rows.append(dict(item, url = reverse('test_view', args = [key, run_id, item['id']]), reason = reason,
                         duration_text = fmt.span(item['duration']) if item.get('duration') is not None else ''))

    counts = run.get('counts') or {}
    total  = counts.get('total') or 0
    here   = reverse('run_view', args = [key, run_id])
    tally = []
    segments = bar_segments(counts)
    for name in ('passed', 'failed', 'error', 'no_expected', 'aborted', 'pending'):
        if counts.get(name):
            tally.append({'key': name, 'count': counts[name], 'label': fmt.label(name), 'url': fmt.with_query(here, status = name)})
    shards = []
    for shard in run.get('shards') or []:
        state = shard.get('state') or 'pending'
        shards.append(dict(shard, state = state, modules_text = ' '.join(shard.get('modules') or []),
                           cls = {'finished': 'passed', 'error': 'error', 'aborted': 'aborted', 'running': 'running'}.get(state, 'pending')))
    sel = run.get('selection')
    selection_info = None
    if sel:
        selection_info = dict(sel, base_short = (sel.get('base') or '')[:8], changed_count = len(sel.get('changed_files') or []),
                              specs_text = ' '.join(sel.get('specs') or []), unmapped_text = ', '.join(sel.get('unmapped') or []))
    failing = [t['id'] for t in run.get('tests', []) if t['status'] in FAILING_STATUS]
    live = run.get('state') in RUNNING_STATES
    context = {
        'run': run, 'live': live, 'total': total, 'done': total - (counts.get('pending') or 0) - (counts.get('running') or 0),
        'segments': segments, 'tally': tally, 'all_url': here, 'shards': shards, 'selection': selection_info,
        'rows': rows, 'has_tests': bool(run.get('tests')), 'q': query, 'status': status,
        'status_options': [{'value': v, 'label': fmt.label(v), 'selected': v == status}
                           for v in ('passed', 'failed', 'error', 'no_expected', 'aborted', 'running', 'pending')],
        'failing_selected': status == 'failing',
        'failing_count': len(failing),
        'rerun_url': fmt.with_query(reverse('new_run', args = [key]), tests = ' '.join(failing), branch = run.get('branch')) if failing else '',
        'message_class': 'error' if run.get('result') in ('error', 'aborted') else 'info',
        'badge': run.get('result') or activity.settled_state(run, activity.buildbot_live() if live else None),
        'short_rev': (run.get('revision') or '-')[:12],
        'duration': fmt.span(fmt.run_duration(run)),
        'started_text': fmt.clock(run.get('started') or run.get('created')),
        'repo': repo, 'index_url': reverse('repo_runs', args = [key]),
        'live_url': fmt.with_query(here, partial = 1, status = status, q = query),
        'api_url': reverse('api_run', args = [key, run_id]),
        'tail_url': reverse('api_tail', args = [key, run_id]),
        'stop': data.get_stop(run_id) if live else None,
        'stop_url': reverse('stop_run', args = [key, run_id]),
        'stop_rights': stop_rights(request.user, run) if request.user.is_authenticated else None,
    }
    return render(request, 'runs/run_live.html' if request.GET.get('partial') else 'runs/run.html', context)


def split_rows(pairs):
    return [{'lno': left[0], 'lcls': left[2], 'ltext': left[1], 'rno': right[0], 'rcls': right[2], 'rtext': right[1]}
            for left, right in pairs]


@require_safe
def test_view(request, key, run_id, test_id):
    repo = get_repo(key)
    run = load_run(repo, run_id)
    if not valid_id(test_id):
        raise Http404()
    item = next((t for t in run.get('tests', []) if t['id'] == test_id), None)
    if not item:
        raise Http404()
    data   = store(repo)
    result = data.get_result(run_id, test_id)
    tdir   = data.test_path(run_id, test_id)
    view   = request.GET.get('view', 'split')
    page   = number(request)
    here   = reverse('test_view', args = [key, run_id, test_id])

    hunks, groups, total_hunks = [], [], 0
    diff_path = os.path.join(tdir, 'diff.txt')
    if result and result['status'] == 'failed' and os.path.exists(diff_path):
        with open(diff_path, 'r', encoding = 'utf8', errors = 'replace', newline = '') as handle:
            parsed = diffparse.parse_unified(handle.read())
        total_hunks = len(parsed)
        summary = result.get('hunks') or []
        if not any(h.get('label') for h in parsed):
            # a diff saved before labels were written into it: only its first regions know their document
            for index, hunk in enumerate(parsed):
                if index < len(summary):
                    hunk['label'] = summary[index].get('label')
        groups = diffparse.group_by_label(parsed)
        start = (page - 1) * DOCS_PER_PAGE
        for doc_no in range(start + 1, min(start + DOCS_PER_PAGE, len(groups)) + 1):
            group = groups[doc_no - 1]
            entry = {'number': doc_no, 'line': group['line'], 'added': group['added'], 'removed': group['removed'],
                     'label': group['label'], 'count': group['count'], 'parts': []}
            for hunk in parsed[group['first']:group['first'] + group['count']]:
                part = {'line': hunk.get('change_line') or hunk.get('old_start'),
                        'added': hunk.get('added'), 'removed': hunk.get('removed')}
                if view == 'split':
                    part['split'] = split_rows(diffparse.side_by_side(hunk))
                else:
                    part['unified'] = [{'tag': tag, 'old': old_no, 'new': new_no, 'sign': SIGNS[tag], 'text': text}
                                       for tag, text, old_no, new_no in hunk['rows'] if tag != 'note']
                entry['parts'].append(part)
            hunks.append(entry)
    pages = max((len(groups) + DOCS_PER_PAGE - 1) // DOCS_PER_PAGE, 1)

    files = {name: os.path.exists(os.path.join(tdir, fname)) for name, fname in RAW_FILES.items()}
    raw   = {kind: reverse('raw_view', args = [key, run_id, test_id, kind]) for kind in RAW_FILES}
    summary_rows = []
    for doc_no, group in enumerate(groups[:MAX_SUMMARY_ROWS], 1):
        summary_rows.append(dict(group, number = doc_no,
                                 url = fmt.with_query(here, page = (doc_no - 1) // DOCS_PER_PAGE + 1, view = view) + '#h%d' % doc_no))
    frames = []
    for index, frame in enumerate(((result or {}).get('error') or {}).get('frames') or []):
        frames.append(dict(frame, last = index == len((result['error']['frames'])) - 1))
    history = [dict(h, url = reverse('test_view', args = [key, h['run'], test_id])) for h in data.history(test_id, before = run_id)]
    diff = (result or {}).get('diff') or {}
    full_files = []
    for kind, label in (('diff', 'Full differences'), ('expected', 'Expected output'), ('actual', 'Actual output')):
        path = os.path.join(tdir, RAW_FILES[kind])
        if os.path.exists(path):
            full_files.append({'label': label, 'size': fmt.size(os.path.getsize(path)), 'path': path,
                               'url': reverse('raw_view', args = [key, run_id, test_id, kind]) + '?download=1',
                               'primary': kind == 'diff'})
    context = {
        'full_files': full_files, 'show_files': bool(full_files) and item['status'] not in ('passed', 'pending', 'running'),
        'run': run, 'item': item, 'result': result, 'files': files, 'raw': raw,
        'repo': repo, 'index_url': reverse('repo_runs', args = [key]), 'run_url': reverse('run_view', args = [key, run_id]),
        'status_label': fmt.label(item['status']),
        'short_rev': (run.get('revision') or '')[:8],
        'duration': fmt.span(item['duration']) if item.get('duration') is not None else '-',
        'expected_size': fmt.size((result or {}).get('expected_bytes')), 'actual_size': fmt.size((result or {}).get('actual_bytes')),
        'waiting': item['status'] in ('pending', 'running'),
        'running': item['status'] == 'running',
        'summary_rows': summary_rows, 'summary_count': len(summary_rows),
        'more_hunks': len(groups) > len(summary_rows),
        'hunks_word': 'region' if diff.get('hunks') == 1 else 'regions',
        'group_total': len(groups),
        'frames': frames, 'reproduce': (result or {}).get('reproduce'), 'baseline': (result or {}).get('baseline'),
        'compares_output': (result or {}).get('kind') == 'diff' or bool((result or {}).get('output')),
        'run_ended': run.get('state') != 'running',
        'reasons': (run.get('selection') or {}).get('reasons', {}).get(test_id, []),
        'history': history, 'hunks': hunks, 'total_hunks': total_hunks, 'page': page, 'pages': pages,
        'split': view == 'split',
        'split_url': fmt.with_query(here, view = 'split', page = page) + '#diff',
        'unified_url': fmt.with_query(here, view = 'unified', page = page) + '#diff',
        'prev_url': fmt.with_query(here, view = view, page = page - 1) + '#diff' if page > 1 else '',
        'next_url': fmt.with_query(here, view = view, page = page + 1) + '#diff' if page < pages else '',
        'tail_url': fmt.with_query(reverse('api_tail', args = [key, run_id]), test = test_id),
        'again_url': fmt.with_query(reverse('new_run', args = [key]), tests = test_id, branch = run.get('branch')),
        'kinds': ('expected', 'actual', 'log'),
    }
    return render(request, 'runs/test.html', context)


@require_safe
def raw_view(request, key, run_id, test_id, kind):
    repo = get_repo(key)
    load_run(repo, run_id)
    if kind not in RAW_FILES or not valid_id(test_id):
        raise Http404()
    path = os.path.join(store(repo).test_path(run_id, test_id), RAW_FILES[kind])
    if not os.path.exists(path):
        raise Http404()
    if request.GET.get('download'):
        return FileResponse(open(path, 'rb'), as_attachment = True, filename = '%s.%s.txt' % (test_id, kind),
                            content_type = 'text/plain')
    total = os.path.getsize(path)
    with open(path, 'rb') as handle:
        body = handle.read(INLINE_LIMIT)
    response = HttpResponse(body, content_type = 'text/plain; charset=utf-8')
    response['X-Total-Bytes'] = str(total)
    response['X-Truncated']   = '1' if total > INLINE_LIMIT else '0'
    return response


@require_safe
def api_run(request, key, run_id):
    return JsonResponse(load_run(get_repo(key), run_id))


@require_safe
def api_tail(request, key, run_id):
    repo    = get_repo(key)
    run     = load_run(repo, run_id)
    test_id = request.GET.get('test') or ''
    try:
        offset = int(request.GET.get('offset', 0))
    except ValueError:
        offset = 0
    if test_id:
        if not valid_id(test_id):
            raise Http404()
        path = os.path.join(store(repo).test_path(run_id, test_id), 'log.txt')
    else:
        path = os.path.join(store(repo).run_path(run_id), 'runner.log')
    text, new_offset = tail_file(path, offset)
    return JsonResponse({'text': text, 'offset': new_offset, 'live': run.get('state') in RUNNING_STATES,
                         'current': run.get('current')})


def compact_specs(chosen, catalog):
    by_module = {}
    for test in catalog['tests']:
        by_module.setdefault(test['module'], set()).add(test['id'])
    chosen_set = set(chosen)
    specs = []
    covered = set()
    for module, ids in by_module.items():
        if ids and ids <= chosen_set:
            specs.append(module)
            covered |= ids
    specs.extend(sorted(chosen_set - covered))
    return ' '.join(specs)


def catalog_error_info(error):
    if not error:
        return None
    return {'message': error.get('message') or '', 'ago': fmt.ago(error.get('at')), 'at_text': fmt.clock(error.get('at'))}


def empty_steps(branch, error, missing_hook, ready):
    if missing_hook:
        where = ' It is already on %s.' % ', '.join(t['name'] for t in ready) if ready else ''
        return [
            {'title': 'Add the hook to %s' % branch, 'current': True,
             'text': 'Merge the commit that adds MODULES to regressions/regression.py.%s' % where},
            {'title': 'Refresh the list', 'text': 'Press the button. Buildbot reads the branch again.'},
            {'title': 'Pick your tests', 'text': 'The modules appear here, ready to queue.'},
        ]
    if error:
        return [
            {'title': 'Read the message above', 'current': True, 'text': 'It says what Buildbot could not do. Also check that the master and a worker of this repository are running.'},
            {'title': 'Refresh the list', 'text': 'Press the button to try again.'},
            {'title': 'Pick your tests', 'text': 'The modules appear here, ready to queue.'},
        ]
    return [
        {'title': 'Press refresh', 'current': True, 'text': 'This asks Buildbot to read %s from the repository.' % branch},
        {'title': 'Wait a minute', 'text': 'A worker fetches the branch and lists its tests.'},
        {'title': 'Reload and pick', 'text': 'Your modules and tests appear on this page.'},
    ]


def repo_branches(repo):
    names = repo.branch_names
    if not names:
        raise Http404()
    return names


@login_required
@require_http_methods(['GET', 'POST'])
def new_run(request, key):
    repo     = get_repo(key)
    config   = repo.to_config()
    branches = repo_branches(repo)
    data     = store(repo)
    if request.method == 'GET':
        branch  = request.GET.get('branch') if request.GET.get('branch') in branches else branches[0]
        catalog = data.catalog(branch)
        preset  = set(selection.split_specs(request.GET.get('tests', '')))
        grouped = {}
        for test in catalog['tests']:
            grouped.setdefault(test['module'], []).append(test)
        modules = []
        for name, tests in grouped.items():
            rows = [dict(t, checked = t['id'] in preset, no_baseline = bool(not t.get('has_expected') and t.get('output'))) for t in tests]
            modules.append({'name': name, 'source': (catalog['modules'].get(name) or {}).get('source', ''),
                            'count': len(tests), 'tests': rows, 'open': any(r['checked'] for r in rows)})
        error = catalog_error_info(data.catalog_error(branch))
        tabs = []
        for name in branches:
            other = catalog if name == branch else data.catalog(name)
            tests = len(other['tests'])
            state = 'ready' if tests else ('failed' if data.catalog_error(name) else 'empty')
            tabs.append({'name': name, 'state': state, 'tests': tests, 'current': name == branch,
                         'url': fmt.with_query(reverse('new_run', args = [key]), branch = name)})
        ready = [t for t in tabs if t['state'] == 'ready' and not t['current']]
        missing_hook = bool(error) and 'regression.MODULES' in error['message']
        return render(request, 'runs/new.html', {
            'repo': repo, 'branch_tabs': tabs, 'ready_branches': ready, 'missing_hook': missing_hook,
            'steps': empty_steps(branch, error, missing_hook, ready),
            'modules': modules, 'catalog': catalog, 'branch': branch,
            'branch_options': options(branches, branch),
            'module_count': len(catalog['modules']), 'test_count': len(catalog['tests']),
            'revision_short': (catalog.get('revision') or '')[:8],
            'configured': bool(settings.BUILDBOT_URL) and repo.enabled,
            'repo_disabled': not repo.enabled,
            'index_url': reverse('repo_runs', args = [key]),
            'new_url': reverse('new_run', args = [key]), 'refresh_url': reverse('refresh_catalog', args = [key]),
            'catalog_error': error,
        })

    mode   = 'all' if request.POST.get('mode') == 'all' else 'explicit'
    branch = (request.POST.get('branch') or branches[0]).strip()
    reason = (request.POST.get('reason') or '').strip()
    who    = request.user.username
    if branch not in branches:
        messages.error(request, 'Branch must be one of: %s' % ', '.join(branches))
        return redirect('new_run', key = key)
    if not repo.enabled:
        messages.error(request, '%s is disabled; enable it before running tests.' % repo.name)
        return redirect('repo_runs', key = key)
    catalog = data.catalog(branch)
    known   = {t['id']: t for t in catalog['tests']}
    chosen  = [i for i in request.POST.getlist('tests') if i in known]
    if mode == 'explicit' and not chosen:
        messages.error(request, 'Select at least one test')
        return redirect(fmt.with_query(reverse('new_run', args = [key]), branch = branch))

    if mode == 'all':
        chosen = [t['id'] for t in catalog['tests']]
        spec_text = 'all'
    else:
        spec_text = compact_specs(chosen, catalog)

    run_id = data.new_ui_run_id()
    run = {'id': run_id, 'state': 'queued', 'result': None, 'trigger': 'manual',
           'repo': key, 'adapter': repo.adapter,
           'branch': branch, 'revision': None,
           'requested_by': who, 'reason': reason, 'build': {'number': None, 'url': None},
           'created': now(), 'started': None, 'finished': None,
           'selection': {'mode': mode, 'specs': spec_text.split(), 'changed_files': [], 'reasons': {}},
           'tests': [{'id': i, 'module': known[i]['module'], 'status': 'pending', 'duration': None} for i in chosen],
           'counts': None, 'current': None, 'message': 'Waiting for a Buildbot worker'}
    run['counts'] = {'total': len(chosen), 'pending': len(chosen), 'passed': 0, 'failed': 0,
                     'error': 0, 'no_expected': 0, 'aborted': 0}
    data.save_run(run_id, run)

    params = {'branch': branch, 'tests': spec_text, 'run_id': run_id, 'requested_by': who,
              'reason': '%s: %s' % (who, reason or 'manual run')}
    try:
        buildbot_client.force_build(settings.BUILDBOT_URL, settings.BUILDBOT_USER, settings.BUILDBOT_PASS,
                                    config.force_scheduler_for(branch), params)
    except buildbot_client.BuildbotError as exc:
        run['state']    = 'finished'
        run['result']   = 'error'
        run['message']  = 'Could not queue the run: %s' % exc
        run['finished'] = now()
        data.save_run(run_id, run)
        messages.error(request, run['message'])
    return redirect('run_view', key = key, run_id = run_id)


def stop_rights(user, run):
    """Who may stop a run: 'own' for the person who started it, 'other' for an administrator (staff) stopping
    somebody else's run or an automatic one, None for everybody else."""
    if run.get('requested_by') == user.username:
        return 'own'
    return 'other' if (user.is_staff or user.is_superuser) else None


@login_required
@require_http_methods(['GET', 'POST'])
def stop_run(request, key, run_id):
    repo   = get_repo(key)
    run    = load_run(repo, run_id)
    back   = redirect('run_view', key = key, run_id = run_id)
    data   = store(repo)
    who    = request.user.username
    rights = stop_rights(request.user, run)
    if run.get('state') not in RUNNING_STATES:
        messages.info(request, 'This run has already ended.')
        return back
    if rights is None:
        messages.error(request, 'Only %s or an administrator can stop this run.' % (run.get('requested_by') or 'the person who started it'))
        return back
    if data.get_stop(run_id):
        messages.info(request, 'A stop was already requested for this run.')
        return back
    if rights == 'other' and request.POST.get('confirm') != 'yes':
        # Somebody else's run (or an automatic one): ask first, naming whose it is.
        return render(request, 'runs/stop_confirm.html', {
            'run': run, 'repo': repo, 'owner': run.get('requested_by') or run.get('trigger') or 'unknown',
            'automatic': run.get('requested_by') == 'Auto', 'stop_url': reverse('stop_run', args = [key, run_id]),
            'run_url': reverse('run_view', args = [key, run_id]), 'index_url': reverse('repo_runs', args = [key]),
            'done': sum(1 for t in run.get('tests', []) if t['status'] not in ('pending', 'running')),
            'total': len(run.get('tests', []))})
    if request.method != 'POST':
        return back
    try:
        outcome = buildbot_client.stop_run(settings.BUILDBOT_URL, settings.BUILDBOT_USER, settings.BUILDBOT_PASS,
                                           run_id, 'Stopped from the website by %s' % who,
                                           parent_builder = repo.to_config().run_builder)
    except buildbot_client.BuildbotError as exc:
        messages.error(request, 'Could not stop the run: %s' % exc)
        return back
    if not outcome['builds'] and not outcome['requests']:
        if activity.settled_state(run, activity.buildbot_live()) == 'stalled':
            data.save_run(run_id, abort_unfinished(run, 'Closed by %s: Buildbot had nothing left for this run' % who))
            messages.info(request, 'Buildbot had nothing left for this run, so it was closed as aborted.')
            return back
        messages.info(request, 'Buildbot has nothing running for this run any more. It is probably just finishing.')
        return back
    if outcome.get('errors'):
        messages.warning(request, 'Buildbot refused part of the request: %s' % outcome['errors'][0])

    data.save_stop(run_id, who)
    if not outcome['parent_started']:
        # The run's own build was still waiting in the queue and is cancelled. No runner will ever write its result.
        for item in run.get('tests', []):
            item['status'] = 'aborted'
        data.save_run(run_id, abort_unfinished(run, 'Stopped by %s before it started' % who))
        messages.info(request, 'The run was cancelled before it started.')
    else:
        messages.info(request, 'Stop requested. Buildbot is stopping %d build%s; unfinished tests will show as aborted.' % (
            len(outcome['builds']), '' if len(outcome['builds']) == 1 else 's'))
    return back


@login_required
@require_POST
def refresh_catalog(request, key):
    repo     = get_repo(key)
    branches = repo_branches(repo)
    branch   = (request.POST.get('branch') or branches[0]).strip()
    who      = request.user.username
    if branch not in branches:
        messages.error(request, 'Branch must be one of: %s' % ', '.join(branches))
        return redirect('new_run', key = key)
    try:
        buildbot_client.force_build(settings.BUILDBOT_URL, settings.BUILDBOT_USER, settings.BUILDBOT_PASS,
                                    repo.to_config().catalog_scheduler,
                                    {'branch': branch, 'reason': '%s: refresh test list' % who})
        messages.info(request, 'Reading the test list of %s from the repository. Reload this page in a minute.' % branch)
    except buildbot_client.BuildbotError as exc:
        messages.error(request, 'Could not start the refresh: %s' % exc)
    return redirect(fmt.with_query(reverse('new_run', args = [key]), branch = branch))


@require_safe
def healthz(request):
    return HttpResponse('ok')


def csrf_failed(request, reason = ''):
    return render(request, 'runs/csrf_failed.html', {'reason': reason, 'public_url': settings.PUBLIC_URL,
                                                     'host': request.get_host()}, status = 403)
