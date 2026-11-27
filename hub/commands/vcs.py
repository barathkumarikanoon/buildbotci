"""Commands about the git side: fetching a branch, and making, removing and sweeping the throwaway checkouts."""
import os

from hub import gitrepo, repos
from hub.commands.context import Context


def cmd_sync(args):
    ctx = Context(args)
    ctx.verify()
    with ctx.git.lock():
        revision = ctx.git.fetch_branch(args.branch)
        last     = ctx.store.last_tested(args.branch)
        action   = 'run'
        if args.trigger == 'push':
            if last and last.get('revision') == revision:
                action = 'skip'
            else:
                ctx.store.set_last_tested(args.branch, revision, None)
    print('BRANCH: %s' % args.branch)
    print('REVISION: %s' % revision)
    print('ACTION: %s' % action)
    if action == 'skip':
        print('No new commits on %s since the last scheduled run (%s). Nothing to run.' % (args.branch, revision[:10]))
    else:
        print('git fetch origin done. Latest commit of %s is %s.' % (args.branch, revision[:10]))
    return 0


def cmd_checkout(args):
    ctx = Context(args, tree = '')
    ctx.verify()
    dest = ctx.git.add_worktree(args.dest, args.revision)
    print('checked out %s at %s' % (args.revision[:10], dest))
    return 0


def cmd_release(args):
    ctx = Context(args, tree = '')
    ctx.verify()
    ctx.git.remove_worktree(args.dest)
    print('released %s' % os.path.abspath(args.dest))
    return 0


def sweep_repos(args):
    path = getattr(args, 'profiles_file', None) or repos.default_snapshot_path()
    configs = repos.load_repos(path)
    if args.repo:
        return [repos.get_repo(path, args.repo)]
    return list(configs.values())


def cmd_sweep(args):
    base = repos.expand_path(args.base)
    configs = sweep_repos(args)
    if args.all:
        busy = set()
    else:
        busy = gitrepo.buildbot_in_use(args.buildbot_url, [c.run_builder for c in configs], [c.shard_builder for c in configs])
        if busy is None:
            print('Cannot ask Buildbot which builds are running (%s); no checkout removed' % args.buildbot_url)
            return 0
    total = 0
    verb = 'would remove' if args.dry_run else 'removed'
    for config in configs:
        git = gitrepo.GitRepo(config)
        if not os.path.isdir(config.repo_dir) or git.git('rev-parse', '--git-dir').returncode != 0:
            continue
        for path in git.sweep(base, busy, dry_run = args.dry_run):
            print('%s old checkout %s' % (verb, path))
            total += 1
    print('%d old checkout%s %s' % (total, '' if total == 1 else 's', 'to remove' if args.dry_run else 'removed'))
    return 0
