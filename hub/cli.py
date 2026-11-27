"""The command line of the CI: `python runner.py [--repo KEY] COMMAND ...`.

Buildbot runs these commands on the workers; developers can run them by hand."""
import argparse
import os
import sys

from hub.commands import catalog, ops, runs, vcs


def get_parser():
    parser = argparse.ArgumentParser(description = 'Runner of the CI: runs the tests of a repo profile')
    parser.add_argument('--repo', default = os.environ.get('CI_REPO'), help = 'key of the repo profile (default: CI_REPO)')
    parser.add_argument('--profiles-file', default = None, help = 'the repo profiles snapshot (default: CI_PROFILES_FILE, else <results folder>/profiles.json)')
    parser.add_argument('--tree', default = None, help = 'a checkout to work in instead of the configured clone (what a build uses)')
    parser.add_argument('--results-dir', default = os.environ.get('CI_RESULTS_DIR'),
                        required = 'CI_RESULTS_DIR' not in os.environ, help = 'default: CI_RESULTS_DIR')
    sub = parser.add_subparsers(dest = 'command', required = True)

    def selecting(p):
        p.add_argument('--tests', default = '', help = 'modules, test ids or globs, space/comma separated')
        p.add_argument('--all', action = 'store_true', help = 'run every test')
        p.add_argument('--base', default = None, help = 'diff base revision for impact selection')
        p.add_argument('--branch', default = None)

    def describing(p):
        p.add_argument('--run-id', default = '')
        p.add_argument('--trigger', default = '')
        p.add_argument('--requested-by', default = '')
        p.add_argument('--reason', default = '')
        p.add_argument('--build-number', default = '')
        p.add_argument('--build-url', default = '')

    run = sub.add_parser('run', help = 'run tests in this process, without Buildbot')
    selecting(run)
    describing(run)
    run.set_defaults(func = runs.cmd_run)

    plan = sub.add_parser('plan', help = 'choose the tests of a run and split them into module builds')
    selecting(plan)
    describing(plan)
    plan.add_argument('--max-shards', type = int, default = 0, help = 'default: the limit of the repo profile')
    plan.set_defaults(func = runs.cmd_plan)

    shard = sub.add_parser('shard', help = 'run one module build of a planned run')
    shard.add_argument('--run-id', required = True)
    shard.add_argument('--shard-index', type = int, required = True)
    shard.add_argument('--worker', default = '')
    shard.add_argument('--build-number', default = '')
    shard.add_argument('--build-url', default = '')
    shard.set_defaults(func = runs.cmd_shard)

    finalize = sub.add_parser('finalize', help = 'merge the module builds into the result of the run')
    finalize.add_argument('--run-id', default = '')
    finalize.set_defaults(func = runs.cmd_finalize)

    select = sub.add_parser('select', help = 'preview which tests would run')
    selecting(select)
    select.set_defaults(func = catalog.cmd_select)

    listing = sub.add_parser('catalog', help = 'list the tests of a branch')
    listing.add_argument('--branch', default = None)
    listing.set_defaults(func = catalog.cmd_catalog)

    sync = sub.add_parser('sync', help = 'git fetch origin for a branch')
    sync.add_argument('--branch', required = True)
    sync.add_argument('--trigger', default = '')
    sync.set_defaults(func = vcs.cmd_sync)

    checkout = sub.add_parser('checkout', help = 'make a throwaway checkout')
    checkout.add_argument('--dest', required = True)
    checkout.add_argument('--revision', required = True)
    checkout.set_defaults(func = vcs.cmd_checkout)

    release = sub.add_parser('release', help = 'remove a throwaway checkout')
    release.add_argument('--dest', required = True)
    release.set_defaults(func = vcs.cmd_release)

    prepare = sub.add_parser('prepare', help = 'the suite-specific step after a checkout')
    prepare.set_defaults(func = ops.cmd_prepare)

    sweep = sub.add_parser('sweep', help = 'remove the checkouts that finished or killed builds left behind (all repos unless --repo)')
    sweep.add_argument('--base', default = os.environ.get('CI_WORKERS_DIR'), required = 'CI_WORKERS_DIR' not in os.environ,
                       help = 'folder that holds the worker folders (default: CI_WORKERS_DIR)')
    sweep.add_argument('--buildbot-url', default = os.environ.get('CI_BUILDBOT_URL'),
                       required = 'CI_BUILDBOT_URL' not in os.environ, help = 'default: CI_BUILDBOT_URL')
    sweep.add_argument('--repo', default = argparse.SUPPRESS, help = 'only this repo profile (also accepted before the command)')
    sweep.add_argument('--all', action = 'store_true', help = 'remove every checkout; only for when Buildbot is stopped')
    sweep.add_argument('--dry-run', action = 'store_true', help = 'only list what would be removed')
    sweep.set_defaults(func = vcs.cmd_sweep)

    prune = sub.add_parser('prune', help = 'delete old runs (all repos unless --repo)')
    prune.add_argument('--days', type = int, required = True, help = 'remove runs that ended more than this many days ago')
    prune.add_argument('--keep', type = int, default = 50, help = 'never remove the newest N runs of a repo (default 50)')
    prune.add_argument('--repo', default = argparse.SUPPRESS, help = 'only this repo profile (also accepted before the command)')
    prune.add_argument('--yes', action = 'store_true', help = 'really delete; without it only lists')
    prune.set_defaults(func = ops.cmd_prune)

    listing_repos = sub.add_parser('repos', help = 'list the repo profiles')
    listing_repos.set_defaults(func = ops.cmd_repos)
    return parser


NEEDS_REPO = {'repos', 'prune', 'sweep'}


def main(argv = None):
    args = get_parser().parse_args(argv)
    if not args.repo and args.command not in NEEDS_REPO:
        raise SystemExit('Say which repo profile to use: --repo KEY (or CI_REPO)')
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
