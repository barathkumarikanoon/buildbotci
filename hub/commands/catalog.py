"""Listing the tests of a branch, and previewing which tests a change would select."""
import os
import shutil
import tempfile

from hub import planning
from hub.commands.context import Context
from hub.store import now

CI_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_catalog(ctx):
    """The tests of the checkout of `ctx`, with the commit they were read at."""
    catalog = ctx.adapter.catalog()
    return dict(catalog, generated = now(), revision = ctx.git.head_revision(), adapter = ctx.repo.adapter)


def read_branch_catalog(ctx, branch):
    """The tests of origin/<branch>, read in a throwaway checkout (so the clone is never touched)."""
    ctx.verify()
    with ctx.git.lock():
        revision = ctx.git.fetch_branch(branch)
    scratch = tempfile.mkdtemp(prefix = 'ci-catalog-')
    try:
        tree = ctx.git.add_worktree(os.path.join(scratch, ctx.repo.checkout_name), revision)
        inner = ctx.for_tree(tree)
        try:
            inner.activate()
            try:
                catalog = build_catalog(inner)
            except RuntimeError as exc:
                raise SystemExit('%s: %s' % (branch, exc))
        finally:
            os.chdir(CI_DIR)
            ctx.git.remove_worktree(tree)
    finally:
        shutil.rmtree(scratch, ignore_errors = True)
    return catalog, revision


def cmd_catalog(args):
    ctx = Context(args)
    if not args.branch:
        ctx.activate()
        try:
            catalog = build_catalog(ctx)
        except RuntimeError as exc:
            raise SystemExit(str(exc))
        branch = ctx.git.current_branch()
        ctx.store.save_catalog(catalog, branch)
        print('catalog: %d modules, %d tests for %s @ %s' % (len(catalog['modules']), len(catalog['tests']),
                                                            branch, (catalog['revision'] or '')[:10]))
        return 0

    try:
        catalog, revision = read_branch_catalog(ctx, args.branch)
    except SystemExit as exc:
        ctx.store.save_catalog_error(args.branch, str(exc.code))
        raise
    except Exception as exc:
        ctx.store.save_catalog_error(args.branch, '%s: %s' % (type(exc).__name__, exc))
        raise
    ctx.store.save_catalog(catalog, args.branch)
    print('catalog: %d modules, %d tests for %s @ %s' % (len(catalog['modules']), len(catalog['tests']),
                                                        args.branch, revision[:10]))
    return 0


def cmd_select(args):
    ctx = Context(args)
    ctx.activate()
    catalog = build_catalog(ctx)
    plan    = planning.make_plan(args.tests, args.all, catalog, ctx.store, ctx.git, ctx.adapter, args.branch, args.base)
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
