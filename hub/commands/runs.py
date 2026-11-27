"""Running tests: a whole run in one process (`run`), or planned (`plan`), run module by module on workers (`shard`)
and merged (`finalize`), which is what Buildbot does."""
import os
import sys
import traceback

from hub import planning
from hub.commands.catalog import build_catalog
from hub.commands.context import Context
from hub.execution import Prefixed, Tee, Terminated, execute_test, install_sigterm_handler, saved_files_lines
from hub.store import ABORTED, PENDING, RUNNING, count_statuses, now


def initial_run(ctx, args, run_id, previous, branch, revision):
    return {
        'id': run_id, 'state': 'running', 'result': None,
        'repo': ctx.repo.key, 'adapter': ctx.repo.adapter,
        'trigger': args.trigger or previous.get('trigger') or 'cli',
        'branch': branch, 'revision': revision,
        'requested_by': args.requested_by or previous.get('requested_by'),
        'reason': args.reason or previous.get('reason'),
        'build': {'number': args.build_number, 'url': args.build_url},
        'created': previous.get('created') or now(), 'started': now(), 'finished': None,
        'selection': None, 'tests': [], 'counts': count_statuses([]),
        'current': None, 'message': None,
    }


def new_run_id(ctx, args):
    return args.run_id or ctx.store.new_run_id('b%s' % args.build_number if args.build_number else 'cli')


def run_items(ctx, session, items, by_id, run_id, save):
    """Run the tests of `items` one after the other, in isolation: a failure never stops the rest."""
    total = len(items)
    for index, item in enumerate(items, 1):
        item['status'] = RUNNING
        save(item['id'])
        print('[%d/%d] %s' % (index, total, item['id']))
        tdir   = ctx.store.test_path(run_id, item['id'])
        result = execute_test(ctx.adapter, session, by_id[item['id']], tdir)
        item['status']   = result['status']
        item['duration'] = result['duration']
        save(None)
        print('    -> %s (%.1fs)%s' % (result['status'], result['duration'], ' - ' + result['reason'] if result['reason'] else ''))
        for line in saved_files_lines(tdir, ctx.repo.key, run_id, item['id'], result['status']):
            print(line)


def summary_line(counts, with_aborted = False):
    text = 'summary: %d total, %d passed, %d failed, %d error, %d no_expected' % (
        counts['total'], counts['passed'], counts['failed'], counts['error'], counts['no_expected'])
    return text + (', %d aborted' % counts['aborted'] if with_aborted else '')


def fail_unmatched(ctx, run_id, run, plan):
    run['state']    = 'finished'
    run['result']   = 'error'
    run['message']  = 'No such test or module: %s' % ' '.join(plan['unmatched_specs'])
    run['finished'] = now()
    ctx.store.save_run(run_id, run)
    print(run['message'])
    return 2


def cmd_run(args):
    ctx = Context(args)
    ctx.activate()
    store   = ctx.store
    run_id  = new_run_id(ctx, args)
    os.makedirs(store.run_path(run_id), exist_ok = True)

    console = open(os.path.join(store.run_path(run_id), 'runner.log'), 'a', encoding = 'utf8', buffering = 1)
    sys.stdout = Tee(sys.__stdout__, console)
    sys.stderr = Tee(sys.__stderr__, console)
    install_sigterm_handler()

    previous = store.get_run(run_id) or {}
    revision = ctx.git.head_revision()
    branch   = args.branch or ctx.git.current_branch()
    run = initial_run(ctx, args, run_id, previous, branch, revision)
    store.save_run(run_id, run)

    ui = os.environ.get('CI_UI_URL')
    print('run %s of %s on %s @ %s' % (run_id, ctx.repo.key, branch, (revision or '')[:10]))
    if ui:
        print('details: %s/r/%s/run/%s' % (ui.rstrip('/'), ctx.repo.key, run_id))

    exit_code = 1
    try:
        catalog = build_catalog(ctx)
        store.save_catalog(catalog, branch)
        plan = planning.make_plan(args.tests, args.all, catalog, store, ctx.git, ctx.adapter, branch, args.base)
        run['selection'] = planning.selection_record(plan)
        if plan['unmatched_specs']:
            return fail_unmatched(ctx, run_id, run, plan)

        by_id = {t['id']: t for t in catalog['tests']}
        run['tests']  = [{'id': i, 'module': by_id[i]['module'], 'status': PENDING, 'duration': None} for i in plan['tests']]
        run['counts'] = count_statuses(run['tests'])
        store.save_run(run_id, run)
        print('%d tests selected (%s)' % (len(plan['tests']), plan['mode']))

        def save(current):
            run['current'] = current
            run['counts']  = count_statuses(run['tests'])
            store.save_run(run_id, run)

        session = ctx.adapter.open()
        run_items(ctx, session, run['tests'], by_id, run_id, save)

        bad = [i for i in run['tests'] if i['status'] != 'passed']
        run['result'] = 'failed' if bad else 'passed'
        run['state']  = 'finished'
        exit_code = 1 if bad else 0
        if not run['tests']:
            run['message'] = 'No tests are impacted by this change'
    except Terminated:
        run['state']   = 'aborted'
        run['result']  = 'aborted'
        run['message'] = 'Run was terminated (Buildbot cancel or timeout)'
        for item in run['tests']:
            if item['status'] == RUNNING:
                item['status'] = ABORTED
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

    if run['state'] == 'finished' and run['result'] in ('passed', 'failed') and run['trigger'] == 'push' and revision:
        store.set_last_tested(branch, revision, run_id)
    print(summary_line(run['counts']))
    return exit_code


def cmd_plan(args):
    ctx = Context(args)
    ctx.activate()
    store    = ctx.store
    run_id   = new_run_id(ctx, args)
    os.makedirs(store.run_path(run_id), exist_ok = True)
    previous = store.get_run(run_id) or {}
    revision = ctx.git.head_revision()
    branch   = args.branch or ctx.git.current_branch()
    run      = initial_run(ctx, args, run_id, previous, branch, revision)
    store.save_run(run_id, run)
    if args.trigger == 'push' and revision:
        store.set_last_tested(branch, revision, run_id)
    print('RUN_ID: %s' % run_id)

    try:
        catalog = build_catalog(ctx)
        store.save_catalog(catalog, branch)
        plan = planning.make_plan(args.tests, args.all, catalog, store, ctx.git, ctx.adapter, branch, args.base)
        run['selection'] = planning.selection_record(plan)
        if plan['unmatched_specs']:
            return fail_unmatched(ctx, run_id, run, plan)

        by_id   = {t['id']: t for t in catalog['tests']}
        modules = []
        for test_id in plan['tests']:
            if by_id[test_id]['module'] not in modules:
                modules.append(by_id[test_id]['module'])
        run['tests']  = [{'id': i, 'module': by_id[i]['module'], 'status': PENDING, 'duration': None} for i in plan['tests']]
        run['counts'] = count_statuses(run['tests'])
        run['shards'] = planning.assign_shards(modules, args.max_shards or ctx.repo.max_shards)
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
    ctx = Context(args)
    ctx.activate()
    store = ctx.store
    run   = store.get_run(args.run_id) if args.run_id else None
    shard = next((s for s in (run or {}).get('shards', []) if s['index'] == args.shard_index), None)
    if not shard:
        print('run %s has no shard %s' % (args.run_id, args.shard_index))
        return 2

    console = open(os.path.join(store.run_path(args.run_id), 'runner.log'), 'a', encoding = 'utf8', buffering = 1)
    prefix  = '[shard %d] ' % shard['index']
    sys.stdout = Tee(sys.__stdout__, Prefixed(console, prefix))
    sys.stderr = Tee(sys.__stderr__, Prefixed(console, prefix))
    install_sigterm_handler()

    data = {'index': shard['index'], 'modules': shard['modules'], 'state': 'running',
            'worker': args.worker or None,
            'build': {'number': args.build_number, 'url': args.build_url},
            'started': now(), 'finished': None, 'current': None, 'message': None, 'tests': []}
    store.save_shard(args.run_id, shard['index'], data)
    print('shard %d of run %s on %s: %s' % (shard['index'], args.run_id, args.worker or 'this worker', ' '.join(shard['modules'])))

    exit_code = 1
    session   = None
    try:
        session = ctx.adapter.open()
        catalog = build_catalog(ctx)
        by_id   = {t['id']: t for t in catalog['tests']}
        ids     = [t['id'] for t in run['tests'] if t['id'] in by_id and by_id[t['id']]['module'] in shard['modules']]
        data['tests'] = [{'id': i, 'status': PENDING, 'duration': None} for i in ids]
        store.save_shard(args.run_id, shard['index'], data)

        def save(current):
            data['current'] = current
            store.save_shard(args.run_id, shard['index'], data)

        run_items(ctx, session, data['tests'], by_id, args.run_id, save)

        bad = [i for i in data['tests'] if i['status'] != 'passed']
        if session.error:
            data['state']   = 'error'
            data['message'] = 'Environment failed to load: %s: %s' % (type(session.error).__name__, session.error)
        else:
            data['state'] = 'finished'
        exit_code = 1 if bad or session.error else 0
    except Terminated:
        data['state']   = 'aborted'
        data['message'] = 'Shard was terminated (Buildbot cancel or timeout)'
        for item in data['tests']:
            if item['status'] == RUNNING:
                item['status'] = ABORTED
    except Exception as exc:
        data['state']   = 'error'
        data['message'] = '%s: %s' % (type(exc).__name__, exc)
        traceback.print_exc()
    finally:
        data['current']  = None
        data['finished'] = now()
        store.save_shard(args.run_id, shard['index'], data)

    print(summary_line(count_statuses(data['tests'])))
    return exit_code


def cmd_finalize(args):
    ctx = Context(args)
    store = ctx.store
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
        if item['status'] in (PENDING, RUNNING):
            shard = shard_of.get(item.get('module') or item['id'].split('.', 1)[0]) or {}
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
        run['message'] = 'No tests are impacted by this change'
    run['counts']   = counts
    run['current']  = None
    run['finished'] = now()
    store.save_run(args.run_id, run)

    print(summary_line(counts, with_aborted = True))
    return 0 if run['result'] == 'passed' else 1
