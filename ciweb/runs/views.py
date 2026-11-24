import os

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from ciweb.runs import buildbot_client, fmt
from hub import diffparse, selection
from hub.store import FAILING_STATUS, RUNNING_STATES, Store, now, valid_id

STATUS_ORDER = {'error': 0, 'failed': 1, 'no_expected': 2, 'aborted': 3,
                'running': 4, 'pending': 5, 'passed': 6}
RAW_FILES    = {'actual': 'actual', 'expected': 'expected', 'diff': 'diff.txt', 'log': 'log.txt'}
INLINE_LIMIT = 256 * 1024
TAIL_LIMIT   = 128 * 1024
DOCS_PER_PAGE  = 15
MAX_SUMMARY_ROWS = 1000
SIGNS = {'add': '+', 'del': '-', 'ctx': ' '}


def store():
    return Store(settings.RESULTS_DIR)


def load_run(run_id):
    if not valid_id(run_id):
        raise Http404()
    run = store().get_run(run_id)
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


def failing_ids_of_latest():
    runs, _ = store().list_runs(limit = 30)
    for run in runs:
        if run.get('state') not in RUNNING_STATES and run.get('tests'):
            ids = [t['id'] for t in run['tests'] if t['status'] in FAILING_STATUS]
            if ids:
                return run['id'], ids
    return None, []


def bar_segments(counts):
    counts = counts or {}
    total = counts.get('total') or 0
    if not total:
        return []
    return [{'key': key, 'width': counts[key] * 100.0 / total}
            for key in ('passed', 'failed', 'error', 'no_expected', 'aborted', 'running') if counts.get(key)]


def options(values, chosen):
    return [{'value': v, 'selected': v == chosen} for v in values]


@require_safe
def index(request):
    branch = request.GET.get('branch') or None
    result = request.GET.get('result') or None
    by     = request.GET.get('by') or None
    page   = number(request)
    per    = 30
    data   = store()
    runs, total = data.list_runs(limit = per, offset = (page - 1) * per, branch = branch, result = result, requested_by = by)
    names  = sorted({r.get('requested_by') for r in data.list_runs(limit = 200)[0] if r.get('requested_by')},
                    key = lambda name: (name != settings.AUTO_NAME, name.lower()))
    rows = []
    for run in runs:
        counts = run.get('counts') or {}
        rows.append(dict(run, url = reverse('run_view', args = [run['id']]),
                         short_rev = (run.get('revision') or '')[:8],
                         duration = fmt.span(fmt.run_duration(run)),
                         started_text = fmt.clock(run.get('started') or run.get('created')),
                         bad = (counts.get('failed') or 0) + (counts.get('error') or 0) + (counts.get('no_expected') or 0),
                         segments = bar_segments(counts),
                         badge = run.get('result') or run.get('state')))
    busy = []
    for run in data.list_runs(limit = 10, state = 'running', recent = 300)[0] + data.list_runs(limit = 10, state = 'queued', recent = 300)[0]:
        counts = run.get('counts') or {}
        busy.append(dict(run, url = reverse('run_view', args = [run['id']]),
                         segments = bar_segments(counts),
                         done = (counts.get('total') or 0) - (counts.get('pending') or 0) - (counts.get('running') or 0)))
    last = []
    for name, info in data.all_last_tested().items():
        finished = data.get_run(info['run']) if info.get('run') else None
        last.append({'branch': name, 'short': (info.get('revision') or '')[:10], 'ago': fmt.ago(info.get('at')),
                     'badge': ((finished or {}).get('result') or (finished or {}).get('state') or ''),
                     'url': reverse('run_view', args = [info['run']]) if info.get('run') else ''})
    params = {'branch': branch, 'result': result, 'by': by}
    return render(request, 'runs/runs.html', {
        'rows': rows, 'total': total, 'plural': '' if total == 1 else 's', 'last': last, 'busy': busy,
        'branch_options': options(settings.BRANCHES, branch),
        'result_options': options(('passed', 'failed', 'error', 'aborted'), result),
        'requester_options': options(names, by),
        'newer': fmt.with_query(reverse('index'), page = page - 1, **params) if page > 1 else '',
        'older': fmt.with_query(reverse('index'), page = page + 1, **params) if page * per < total else '',
    })


@require_safe
def run_view(request, run_id):
    run    = load_run(run_id)
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
    data = store()
    rows = []
    for item in tests:
        reason = ''
        if item['status'] in FAILING_STATUS:
            reason = (data.get_result(run_id, item['id']) or {}).get('reason') or ''
        rows.append(dict(item, url = reverse('test_view', args = [run_id, item['id']]), reason = reason,
                         duration_text = fmt.span(item['duration']) if item.get('duration') is not None else ''))

    counts = run.get('counts') or {}
    total  = counts.get('total') or 0
    here   = reverse('run_view', args = [run_id])
    tally = []
    segments = bar_segments(counts)
    for key in ('passed', 'failed', 'error', 'no_expected', 'aborted', 'pending'):
        if counts.get(key):
            tally.append({'key': key, 'count': counts[key], 'label': fmt.label(key), 'url': fmt.with_query(here, status = key)})
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
        'rerun_url': fmt.with_query(reverse('new_run'), tests = ' '.join(failing), branch = run.get('branch')) if failing else '',
        'message_class': 'error' if run.get('result') in ('error', 'aborted') else 'info',
        'badge': run.get('result') or run.get('state'),
        'short_rev': (run.get('revision') or '-')[:12],
        'duration': fmt.span(fmt.run_duration(run)),
        'started_text': fmt.clock(run.get('started') or run.get('created')),
        'index_url': reverse('index'),
        'live_url': fmt.with_query(here, partial = 1, status = status, q = query),
        'api_url': reverse('api_run', args = [run_id]),
        'tail_url': reverse('api_tail', args = [run_id]),
        'stop': data.get_stop(run_id) if live else None,
        'stop_url': reverse('stop_run', args = [run_id]),
    }
    return render(request, 'runs/run_live.html' if request.GET.get('partial') else 'runs/run.html', context)


def split_rows(pairs):
    return [{'lno': left[0], 'lcls': left[2], 'ltext': left[1], 'rno': right[0], 'rcls': right[2], 'rtext': right[1]}
            for left, right in pairs]


@require_safe
def test_view(request, run_id, test_id):
    run = load_run(run_id)
    if not valid_id(test_id):
        raise Http404()
    item = next((t for t in run.get('tests', []) if t['id'] == test_id), None)
    if not item:
        raise Http404()
    data   = store()
    result = data.get_result(run_id, test_id)
    tdir   = data.test_path(run_id, test_id)
    view   = request.GET.get('view', 'split')
    page   = number(request)
    here   = reverse('test_view', args = [run_id, test_id])

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
    raw   = {kind: reverse('raw_view', args = [run_id, test_id, kind]) for kind in RAW_FILES}
    summary_rows = []
    for doc_no, group in enumerate(groups[:MAX_SUMMARY_ROWS], 1):
        summary_rows.append(dict(group, number = doc_no,
                                 url = fmt.with_query(here, page = (doc_no - 1) // DOCS_PER_PAGE + 1, view = view) + '#h%d' % doc_no))
    frames = []
    for index, frame in enumerate(((result or {}).get('error') or {}).get('frames') or []):
        frames.append(dict(frame, last = index == len((result['error']['frames'])) - 1))
    history = [dict(h, url = reverse('test_view', args = [h['run'], test_id])) for h in data.history(test_id, before = run_id)]
    diff = (result or {}).get('diff') or {}
    full_files = []
    for kind, label in (('diff', 'Full diff'), ('expected', 'Expected output'), ('actual', 'Actual output')):
        path = os.path.join(tdir, RAW_FILES[kind])
        if os.path.exists(path):
            full_files.append({'label': label, 'size': fmt.size(os.path.getsize(path)), 'path': path,
                               'url': reverse('raw_view', args = [run_id, test_id, kind]) + '?download=1',
                               'primary': kind == 'diff'})
    context = {
        'full_files': full_files, 'show_files': bool(full_files) and item['status'] not in ('passed', 'pending', 'running'),
        'run': run, 'item': item, 'result': result, 'files': files, 'raw': raw,
        'index_url': reverse('index'), 'run_url': reverse('run_view', args = [run_id]),
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
        'frames': frames, 'profile': run.get('profile') or 'local',
        'run_ended': run.get('state') != 'running',
        'reasons': (run.get('selection') or {}).get('reasons', {}).get(test_id, []),
        'history': history, 'hunks': hunks, 'total_hunks': total_hunks, 'page': page, 'pages': pages,
        'split': view == 'split',
        'split_url': fmt.with_query(here, view = 'split', page = page) + '#diff',
        'unified_url': fmt.with_query(here, view = 'unified', page = page) + '#diff',
        'prev_url': fmt.with_query(here, view = view, page = page - 1) + '#diff' if page > 1 else '',
        'next_url': fmt.with_query(here, view = view, page = page + 1) + '#diff' if page < pages else '',
        'tail_url': fmt.with_query(reverse('api_tail', args = [run_id]), test = test_id),
        'again_url': fmt.with_query(reverse('new_run'), tests = test_id, branch = run.get('branch')),
        'kinds': ('expected', 'actual', 'log'),
    }
    return render(request, 'runs/test.html', context)


@require_safe
def raw_view(request, run_id, test_id, kind):
    load_run(run_id)
    if kind not in RAW_FILES or not valid_id(test_id):
        raise Http404()
    path = os.path.join(store().test_path(run_id, test_id), RAW_FILES[kind])
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
def api_run(request, run_id):
    return JsonResponse(load_run(run_id))


@require_safe
def api_tail(request, run_id):
    run     = load_run(run_id)
    test_id = request.GET.get('test') or ''
    try:
        offset = int(request.GET.get('offset', 0))
    except ValueError:
        offset = 0
    if test_id:
        if not valid_id(test_id):
            raise Http404()
        path = os.path.join(store().test_path(run_id, test_id), 'log.txt')
    else:
        path = os.path.join(store().run_path(run_id), 'runner.log')
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
            {'title': 'Check Buildbot', 'current': True, 'text': 'The master and a worker must be running.'},
            {'title': 'Refresh the list', 'text': 'Press the button to try again.'},
            {'title': 'Pick your tests', 'text': 'The modules appear here, ready to queue.'},
        ]
    return [
        {'title': 'Press refresh', 'current': True, 'text': 'This asks Buildbot to read %s from GitLab.' % branch},
        {'title': 'Wait a minute', 'text': 'A worker fetches the branch and lists its tests.'},
        {'title': 'Reload and pick', 'text': 'Your modules and tests appear on this page.'},
    ]


@login_required
@require_http_methods(['GET', 'POST'])
def new_run(request):
    data = store()
    if request.method == 'GET':
        branch  = request.GET.get('branch') if request.GET.get('branch') in settings.BRANCHES else settings.BRANCHES[0]
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
        source, failing = failing_ids_of_latest()
        error = catalog_error_info(data.catalog_error(branch))
        tabs = []
        for name in settings.BRANCHES:
            other = catalog if name == branch else data.catalog(name)
            tests = len(other['tests'])
            state = 'ready' if tests else ('failed' if data.catalog_error(name) else 'empty')
            tabs.append({'name': name, 'state': state, 'tests': tests, 'current': name == branch,
                         'url': fmt.with_query(reverse('new_run'), branch = name)})
        ready = [t for t in tabs if t['state'] == 'ready' and not t['current']]
        missing_hook = bool(error) and 'regression.MODULES' in error['message']
        return render(request, 'runs/new.html', {
            'branch_tabs': tabs, 'ready_branches': ready, 'missing_hook': missing_hook,
            'steps': empty_steps(branch, error, missing_hook, ready),
            'modules': modules, 'catalog': catalog, 'branch': branch,
            'branch_options': options(settings.BRANCHES, branch),
            'module_count': len(catalog['modules']), 'test_count': len(catalog['tests']),
            'revision_short': (catalog.get('revision') or '')[:8],
            'failing': ' '.join(failing), 'failing_count': len(failing), 'failing_run': source,
            'configured': bool(settings.BUILDBOT_URL),
            'new_url': reverse('new_run'), 'refresh_url': reverse('refresh_catalog'),
            'catalog_error': error,
        })

    mode   = 'all' if request.POST.get('mode') == 'all' else 'explicit'
    branch = (request.POST.get('branch') or settings.BRANCHES[0]).strip()
    reason = (request.POST.get('reason') or '').strip()
    who    = request.user.username
    if branch not in settings.BRANCHES:
        messages.error(request, 'Branch must be one of: %s' % ', '.join(settings.BRANCHES))
        return redirect('new_run')
    catalog = data.catalog(branch)
    known   = {t['id']: t for t in catalog['tests']}
    chosen  = [i for i in request.POST.getlist('tests') if i in known]
    if mode == 'explicit' and not chosen:
        messages.error(request, 'Select at least one test')
        return redirect(fmt.with_query(reverse('new_run'), branch = branch))

    if mode == 'all':
        chosen = [t['id'] for t in catalog['tests']]
        spec_text = 'all'
    else:
        spec_text = compact_specs(chosen, catalog)

    run_id = data.new_ui_run_id()
    run = {'id': run_id, 'state': 'queued', 'result': None, 'trigger': 'manual',
           'branch': branch, 'revision': None, 'profile': None,
           'requested_by': who, 'reason': reason, 'build': {'number': None, 'url': None},
           'created': now(), 'started': None, 'finished': None,
           'selection': {'mode': mode, 'specs': spec_text.split(), 'changed_files': [], 'reasons': {}},
           'tests': [{'id': i, 'status': 'pending', 'duration': None} for i in chosen],
           'counts': None, 'current': None, 'message': 'Waiting for a Buildbot worker'}
    run['counts'] = {'total': len(chosen), 'pending': len(chosen), 'passed': 0, 'failed': 0,
                     'error': 0, 'no_expected': 0, 'aborted': 0}
    data.save_run(run_id, run)

    params = {'branch': branch, 'tests': spec_text, 'run_id': run_id, 'requested_by': who,
              'reason': '%s: %s' % (who, reason or 'manual run')}
    try:
        buildbot_client.force_build(settings.BUILDBOT_URL, settings.BUILDBOT_USER, settings.BUILDBOT_PASS,
                                    settings.FORCE_SCHEDULER, params)
    except buildbot_client.BuildbotError as exc:
        run['state']    = 'finished'
        run['result']   = 'error'
        run['message']  = 'Could not queue the run: %s' % exc
        run['finished'] = now()
        data.save_run(run_id, run)
        messages.error(request, run['message'])
    return redirect('run_view', run_id = run_id)


@login_required
@require_POST
def stop_run(request, run_id):
    run   = load_run(run_id)
    back  = redirect('run_view', run_id = run_id)
    data  = store()
    who   = request.user.username
    if run.get('state') not in RUNNING_STATES:
        messages.info(request, 'This run has already ended.')
        return back
    if data.get_stop(run_id):
        messages.info(request, 'A stop was already requested for this run.')
        return back
    try:
        outcome = buildbot_client.stop_run(settings.BUILDBOT_URL, settings.BUILDBOT_USER, settings.BUILDBOT_PASS,
                                           run_id, 'Stopped from the web UI by %s' % who)
    except buildbot_client.BuildbotError as exc:
        messages.error(request, 'Could not stop the run: %s' % exc)
        return back
    if not outcome['builds'] and not outcome['requests']:
        messages.info(request, 'Buildbot has nothing running for this run any more. It is probably just finishing.')
        return back

    data.save_stop(run_id, who)
    if not outcome['parent_started']:
        # The run's own build was still waiting in the queue and is cancelled. No runner will ever write its result.
        for item in run.get('tests', []):
            item['status'] = 'aborted'
        total = len(run.get('tests', []))
        run.update({'state': 'aborted', 'result': 'aborted', 'finished': now(), 'current': None,
                    'message': 'Stopped by %s before it started' % who,
                    'counts': {'total': total, 'pending': 0, 'passed': 0, 'failed': 0, 'error': 0,
                               'no_expected': 0, 'aborted': total}})
        data.save_run(run_id, run)
        messages.info(request, 'The run was cancelled before it started.')
    else:
        messages.info(request, 'Stop requested. Buildbot is stopping %d build%s; unfinished tests will show as aborted.' % (
            len(outcome['builds']), '' if len(outcome['builds']) == 1 else 's'))
    return back


@login_required
@require_POST
def refresh_catalog(request):
    branch = (request.POST.get('branch') or settings.BRANCHES[0]).strip()
    who    = request.user.username
    if branch not in settings.BRANCHES:
        messages.error(request, 'Branch must be one of: %s' % ', '.join(settings.BRANCHES))
        return redirect('new_run')
    try:
        buildbot_client.force_build(settings.BUILDBOT_URL, settings.BUILDBOT_USER, settings.BUILDBOT_PASS,
                                    settings.CATALOG_SCHEDULER,
                                    {'branch': branch, 'reason': '%s: refresh test list' % who})
        messages.info(request, 'Reading the test list of %s from GitLab. Reload this page in a minute.' % branch)
    except buildbot_client.BuildbotError as exc:
        messages.error(request, 'Could not start the refresh: %s' % exc)
    return redirect(fmt.with_query(reverse('new_run'), branch = branch))


@require_safe
def healthz(request):
    return HttpResponse('ok')


def csrf_failed(request, reason = ''):
    return render(request, 'runs/csrf_failed.html', {'reason': reason, 'public_url': settings.PUBLIC_URL,
                                                     'host': request.get_host()}, status = 403)
