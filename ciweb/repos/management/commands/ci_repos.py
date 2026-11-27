import os
import shutil
import subprocess
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.utils import OperationalError, ProgrammingError

from hub import repos as hub_repos
from ciweb.repos import publish
from ciweb.repos.models import Branch, RepoProfile

RESULT_PARTS = ('runs', 'state', 'catalogs')


class Command(BaseCommand):
    help = ('Repository profiles from the command line. '
            'list | export | workers [KEY] [--create] | remove KEY [--yes] | remove-workers [KEY] [--worker NAME] [--orphans] [--yes] | start-workers|stop-workers|restart-workers|status-workers [KEY] [--worker NAME] | seed-legacy [--dry-run] [--no-move]')

    def add_arguments(self, parser):
        parser.add_argument('action', choices = ['list', 'export', 'workers', 'remove', 'remove-workers', 'start-workers', 'stop-workers', 'restart-workers', 'status-workers', 'seed-legacy'])
        parser.add_argument('key', nargs = '?', help = 'a repository profile (default: all)')
        parser.add_argument('--create', action = 'store_true', help = 'workers: create the ones that do not exist yet')
        parser.add_argument('--dry-run', action = 'store_true')
        parser.add_argument('--yes', action = 'store_true', help = 'remove: really do it (without it only shows what would happen)')
        parser.add_argument('--keep-workers', action = 'store_true', help = 'remove: leave the worker folders on this machine alone')
        parser.add_argument('--erase-results', action = 'store_true', help = 'remove: also delete the results folder of the repository')
        parser.add_argument('--erase-history', action = 'store_true', help = "remove: also erase Buildbot's history of the repository's builds")
        parser.add_argument('--worker', action = 'append', default = [], metavar = 'NAME', help = 'remove-workers: a worker to delete (repeat for several)')
        parser.add_argument('--orphans', action = 'store_true', help = 'remove-workers: also delete the worker folders that no repository lists any more')
        parser.add_argument('--shrink', action = 'store_true', help = 'remove-workers: also take the deleted workers out of their repository (the master then drops them)')
        parser.add_argument('--keep-profile', action = 'store_true', help = 'remove-workers: delete only the folders; the repository still lists the workers (bin/create-workers makes them again)')
        parser.add_argument('--force', action = 'store_true', help = 'remove-workers, stop-workers, restart-workers: act even if the worker is running a build, or Buildbot cannot be asked')
        parser.add_argument('--systemd', action = 'store_true', help = 'start/stop/restart/status-workers: use systemctl (default: whichever is installed)')
        parser.add_argument('--direct', action = 'store_true', help = 'start/stop/restart/status-workers: use buildbot-worker on the folder, not systemd')
        parser.add_argument('--no-move', action = 'store_true', help = 'seed-legacy: do not move the old results into the new folder')

    def handle(self, *args, **options):
        try:
            getattr(self, 'do_' + options['action'].replace('-', '_'))(options)
        except (ProgrammingError, OperationalError) as problem:
            if 'ci_repos' in str(problem) and 'does not exist' in str(problem) or 'no such table' in str(problem):
                raise CommandError('The repository tables do not exist yet. Run  python manage.py migrate  (or bin/apply-update) first.')
            raise

    def profiles(self, options):
        found = RepoProfile.objects.filter(key = options['key']) if options.get('key') else RepoProfile.objects.all()
        if options.get('key') and not found:
            raise CommandError('No repository profile named %s' % options['key'])
        return found

    def do_list(self, options):
        for p in self.profiles(options):
            self.stdout.write('%-16s %-9s %-10s branches: %s  workers: %s' % (
                p.key, 'enabled' if p.enabled else 'disabled', p.adapter, ','.join(p.branch_names), ','.join(p.worker_list())))

    def do_export(self, options):
        if not settings.PROFILES_FILE:
            raise CommandError('Set CI_RESULTS_DIR or CI_PROFILES_FILE')
        changed = publish.publish()
        self.stdout.write('%s %s' % ('written' if changed else 'already up to date:', settings.PROFILES_FILE))

    # ---- workers ---------------------------------------------------------------------------------------------

    def do_workers(self, options):
        from ciweb.repos import workerctl
        if not settings.WORKERS_DIR:
            raise CommandError('CI_WORKERS_DIR is not set')
        missing = []
        for profile in self.profiles(options):
            for name in profile.worker_list():
                folder = os.path.join(settings.WORKERS_DIR, name)
                if os.path.exists(os.path.join(folder, 'buildbot.tac')):
                    if options['create'] and workerctl.info_is_placeholder(folder):
                        workerctl.write_info(folder, name, profile)
                        self.stdout.write('exists   %s (its description in Buildbot was Buildbot\'s placeholder; updated, restart the worker to show it)' % name)
                    else:
                        self.stdout.write('exists   %s' % name)
                    continue
                missing.append(name)
                if not options['create']:
                    self.stdout.write('missing  %s' % name)
                    continue
                try:
                    workerctl.create_worker(profile, name)
                except workerctl.WorkerError as exc:
                    raise CommandError(str(exc))
                self.stdout.write('created  %s' % name)
        if missing:
            verb = 'Start them' if options['create'] else 'Create them with  bin/create-workers  and start them'
            self.stdout.write('\n%s:  bin/workers start%s' % (verb, ' ' + options['key'] if options.get('key') else ''))

    # ---- from the settings of before there were repo profiles ----------------------------------------------

    def do_remove(self, options):
        from ciweb.repos import decommission
        if not options.get('key'):
            raise CommandError('Say which repository: manage.py ci_repos remove KEY')
        profile = list(self.profiles(options))[0]
        facts   = decommission.describe(profile.to_config())
        self.stdout.write('Repository %s: %d run(s) in progress, workers %s (on this machine: %s), results %s' % (
            profile.key, facts['open_runs'], ','.join(facts['workers']) or '-', ','.join(facts['local_workers']) or 'none',
            facts['results_dir'] if facts['has_results'] else 'none'))
        if not options['yes']:
            self.stdout.write('Nothing changed. Add --yes to stop its runs, delete the profile and clean up.')
            return
        for line in decommission.retire(profile, 'manage.py', workers = not options['keep_workers'],
                                        results = options['erase_results'], history = options['erase_history']):
            self.stdout.write(line)

    def do_remove_workers(self, options):
        from ciweb.repos import decommission
        if options['shrink'] and options['keep_profile']:
            raise CommandError('Use either --shrink or --keep-profile, not both.')
        if not settings.WORKERS_DIR:
            raise CommandError('CI_WORKERS_DIR is not set')
        owners    = decommission.worker_owners()
        installed = decommission.installed_workers()
        names     = []
        if options.get('key'):
            profile = list(self.profiles(options))[0]
            listed  = profile.worker_list()
            for name in options['worker']:
                if name not in listed:
                    raise CommandError('%s is not a worker of %s (its workers: %s)' % (name, profile.key, ', '.join(listed)))
            names += options['worker'] or listed
        else:
            for name in options['worker']:
                if name not in owners and name not in installed:
                    raise CommandError('No worker named %s: no repository lists it and there is no folder for it.' % name)
            names += options['worker']
        if options['orphans']:
            names += [n for n in installed if n not in owners]
        if not names:
            raise CommandError('Say what to delete: KEY (all its workers), --worker NAME, or --orphans.\n'
                               'Workers here: ' + ', '.join('%s%s' % (n, '' if n in owners else ' (not listed by any repository)') for n in installed))
        names = list(dict.fromkeys(names))
        mode  = 'shrink' if options['shrink'] else 'keep' if options['keep_profile'] else ''
        report, removed = decommission.remove_named_workers(names, mode, force = options['force'], dry_run = not options['yes'])
        for line in report:
            self.stdout.write(line)
        if not options['yes']:
            self.stdout.write('Nothing changed. Add --yes to do it.')
        elif removed:
            self.stdout.write('Deleted %d worker folder(s). Workers started under systemd: also  sudo systemctl disable --now %s' % (
                len(removed), ' '.join('buildbot-worker@%s' % n for n in removed)))

    def do_start_workers(self, options):
        self.control('start', options)

    def do_stop_workers(self, options):
        self.control('stop', options)

    def do_restart_workers(self, options):
        self.control('restart', options)

    def do_status_workers(self, options):
        self.control('status', options)

    def control(self, action, options):
        """KEY alone: all its workers; --worker NAME: those; nothing given: every worker folder on this machine."""
        from ciweb.repos import workerctl
        if options['systemd'] and options['direct']:
            raise CommandError('Use either --systemd or --direct, not both.')
        if not settings.WORKERS_DIR:
            raise CommandError('CI_WORKERS_DIR is not set')
        from ciweb.repos import decommission
        owners    = decommission.worker_owners()
        installed = decommission.installed_workers()
        if options.get('key'):
            profile = list(self.profiles(options))[0]
            listed  = profile.worker_list()
            for name in options['worker']:
                if name not in listed:
                    raise CommandError('%s is not a worker of %s (its workers: %s)' % (name, profile.key, ', '.join(listed)))
            names = options['worker'] or listed
        elif options['worker']:
            names = options['worker']
            for name in names:
                if name not in owners and name not in installed:
                    raise CommandError('No worker named %s: no repository lists it and there is no folder for it.' % name)
        else:
            names = sorted(set(installed) | set(owners))
        mode = 'systemd' if options['systemd'] else 'direct' if options['direct'] else 'auto'
        report, failed = workerctl.control(action, list(dict.fromkeys(names)), mode, force = options['force'], dry_run = options['dry_run'])
        for line in report:
            self.stdout.write(line)
        if failed:
            raise CommandError('%d worker(s) could not be %s.' % (failed, {'start': 'started', 'stop': 'stopped', 'restart': 'restarted'}.get(action, 'checked')))

    def do_seed_legacy(self, options):
        """Makes the first repo profile, `indianlaw`, from the old settings (CI_BRANCHES, CI_PYTHON, CI_WORKERS,
        CI_REGRESSION_PROFILE, ...) and moves the results of before into its own folder. Safe to run once."""
        env = os.environ
        if RepoProfile.objects.filter(key = 'indianlaw').exists():
            raise CommandError('The repository profile indianlaw already exists.')
        branches = [b.strip() for b in env.get('CI_BRANCHES', '').split(',') if b.strip()]
        if not branches:
            raise CommandError('CI_BRANCHES is not set: there is nothing to seed from. Add the repository in the website instead.')
        ci_dir  = hub_repos.expand_path(env.get('CI_DIR', os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))))
        repo_dir = hub_repos.expand_path(env.get('CI_REPO_DIR') or os.path.join(os.path.dirname(ci_dir), 'indianlaw'))
        options_ = {'env_profile': env.get('CI_REGRESSION_PROFILE', 'local'), 'profile_file': env.get('CI_PROFILE_FILE', '')}
        workers = [w.strip() for w in env.get('CI_WORKERS', '').split(',') if w.strip()]
        profile = RepoProfile(
            key = 'indianlaw', name = 'Indian Law', description = 'Regression tests of the indianlaw application',
            adapter = 'comparison', options = options_, repo_dir = repo_dir, checkout_name = 'indianlaw',
            python = env.get('CI_PYTHON', 'python3'),
            cron_hour = env.get('CI_CRON_HOUR', '21'), cron_minute = env.get('CI_CRON_MINUTE', '0'),
            worker_count = len(workers) or 3, worker_names = ','.join(workers),
            max_runs = int(env.get('CI_MAX_RUNS', '8')), max_shards = int(env.get('CI_MAX_SHARDS', '32')),
            max_parallel = int(env.get('CI_MAX_PARALLEL_SHARDS', '0')),
            step_timeout = int(env.get('CI_STEP_TIMEOUT', '7200')), step_max_time = int(env.get('CI_STEP_MAX_TIME', '21600')))
        target = os.path.join(settings.RESULTS_DIR, 'indianlaw')
        moves  = [(os.path.join(settings.RESULTS_DIR, part), os.path.join(target, part)) for part in RESULT_PARTS
                  if os.path.isdir(os.path.join(settings.RESULTS_DIR, part)) and not os.path.exists(os.path.join(target, part))]
        self.stdout.write('Will create the repository profile indianlaw: clone %s, python %s, branches %s, workers %s' % (
            repo_dir, profile.python, ','.join(branches), ','.join(profile.worker_list())))
        if not options['no_move']:
            for source, dest in moves:
                self.stdout.write('Will move %s -> %s' % (source, dest))
        if options['dry_run']:
            self.stdout.write('Dry run: nothing changed.')
            return
        profile.save()
        for position, name in enumerate(branches):
            Branch.objects.create(repo = profile, name = name, position = position)
        if not options['no_move']:
            os.makedirs(target, exist_ok = True)
            for source, dest in moves:
                shutil.move(source, dest)
        self.stdout.write(self.style.SUCCESS('Done. Stop the old master, copy the new master.cfg and start it (see CLAUDE.md).'))
