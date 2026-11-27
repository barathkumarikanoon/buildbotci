"""Housekeeping commands."""
import os

from hub import repos
from hub.commands.context import Context
from hub.store import Store


def cmd_prune(args):
    path    = getattr(args, 'profiles_file', None) or repos.default_snapshot_path()
    configs = [repos.get_repo(path, args.repo)] if args.repo else list(repos.load_repos(path, only_enabled = False).values())
    total, size_total = 0, 0
    for config in configs:
        store = Store(config.results_dir(args.results_dir))
        names, size = store.prune(args.days, keep_latest = args.keep, dry_run = not args.yes)
        for name in names:
            print('%s %s/%s' % ('removed' if args.yes else 'would remove', config.key, name))
        total += len(names)
        size_total += size
    print('%d run%s %s, %.1f MB' % (total, '' if total == 1 else 's', 'removed' if args.yes else 'to remove', size_total / 1048576))
    if not args.yes and total:
        print('Nothing was deleted. Add --yes to delete them.')
    return 0


def cmd_prepare(args):
    ctx = Context(args)
    ctx.verify()
    os.environ.update(ctx.repo.env)
    print(ctx.adapter.prepare_checkout() or 'nothing to prepare')
    return 0


def cmd_repos(args):
    path = getattr(args, 'profiles_file', None) or repos.default_snapshot_path()
    for config in repos.load_repos(path, only_enabled = False).values():
        print('%s\t%s\t%s\t%s' % (config.key, 'enabled' if config.enabled else 'disabled', config.adapter, ','.join(config.workers)))
    return 0
