"""What the Buildbot master needs for ONE repo profile: its workers, schedulers and builders.

master.cfg calls `build_repo` for every enabled profile in the snapshot, so a new repo needs no change here: it is
added in the web UI, and the master picks it up (see watcher.py). Every name comes from RepoConfig, so the master,
the web UI and the runner agree on them.
"""
import functools
import os
from types import SimpleNamespace

from buildbot.plugins import schedulers, steps, util, worker

from hub.adapters import adapter_class

# The environment of a step contains the passwords of this master. Never write it into the build logs:
# the logs are readable without signing in.
ShellCommand           = functools.partial(steps.ShellCommand, logEnviron = False)
SetPropertyFromCommand = functools.partial(steps.SetPropertyFromCommand, logEnviron = False)

BRANCH   = util.Interpolate('%(prop:branch)s')
REVISION = util.Interpolate('%(prop:tested_revision)s')
RUN_DIR  = util.Interpolate('run-%(prop:buildnumber)s')


def settings_from_environment(environ, path_setting):
    """The settings every repo shares (they come from the master's settings file)."""
    return SimpleNamespace(
        ci_runner      = os.path.join(path_setting('CI_DIR', required = True), 'runner.py'),
        results_dir    = path_setting('CI_RESULTS_DIR', required = True),
        profiles_file  = '',
        ui_url         = environ.get('CI_UI_URL', ''),
        workers_dir    = path_setting('CI_WORKERS_DIR', required = True),
        internal_url   = environ['CI_BUILDBOT_URL'],
        worker_password = environ['BUILDBOT_WORKER_PASSWORD'],
    )


def cron_value(text):
    text = str(text).strip()
    return int(text) if text.isdigit() else text


def build_repo(repo, g):
    """(workers, schedulers, builders) of one repo profile. `g` holds the settings every repo shares."""
    adapter = adapter_class(repo.adapter)
    env = {'CI_RESULTS_DIR': g.results_dir, 'CI_PROFILES_FILE': g.profiles_file, 'CI_UI_URL': g.ui_url,
           'PYTHONUNBUFFERED': '1'}
    checkout = repo.checkout_name
    run_tree = util.Interpolate('run-%%(prop:buildnumber)s/%s' % checkout)

    def runner(*arguments, tree = None):
        command = [repo.python, g.ci_runner, '--repo', repo.key]
        if tree:
            command += ['--tree', tree]
        return command + list(arguments)

    def scheduled(props):
        return (props.getProperty('scheduler') or '').startswith(repo.nightly_prefix)

    @util.renderer
    def trigger_name(props):
        return 'push' if scheduled(props) else 'manual'

    @util.renderer
    def requested_by_arg(props):
        if scheduled(props):
            return 'Auto'
        return props.getProperty('requested_by') or props.getProperty('owner') or ''

    @util.renderer
    def reason_arg(props):
        if scheduled(props):
            return 'Scheduled run (%s)' % props.getProperty('scheduler')
        reason = props.getProperty('reason') or ''
        who    = props.getProperty('requested_by') or ''
        prefix = '%s: ' % who
        if who and reason.startswith(prefix):
            return reason[len(prefix):]
        return reason

    @util.renderer
    def tests_arg(props):
        if scheduled(props):
            return 'all'
        return (props.getProperty('tests') or '').strip() or 'all'

    def extract_sync(rc, stdout, stderr):
        found = {}
        for line in stdout.splitlines():
            if line.startswith('REVISION: '):
                found['tested_revision'] = line[len('REVISION: '):].strip()
            elif line.startswith('ACTION: '):
                found['sync_action'] = line[len('ACTION: '):].strip()
        return found

    def extract_plan(rc, stdout, stderr):
        found = {}
        for line in stdout.splitlines():
            if line.startswith('RUN_ID: '):
                found['plan_run_id'] = line[len('RUN_ID: '):].strip()
            elif line.startswith('SHARDS: '):
                found['shard_count'] = int(line[len('SHARDS: '):].strip())
        return found

    class ShardTrigger(steps.Trigger):
        def getSchedulersAndProperties(self):
            count = int(self.getProperty('shard_count', 0) or 0)
            return [{'sched_name': repo.shard_scheduler(index), 'props_to_set': self.set_properties,
                     'unimportant': False} for index in range(1, count + 1)]

    def wanted(step):
        return step.getProperty('sync_action') == 'run'

    def has_shards(step):
        return int(step.getProperty('shard_count', 0) or 0) > 0

    def has_run(step):
        return bool(step.getProperty('plan_run_id') or step.getProperty('run_id'))

    # ---- the build of a run: fetch, check out, plan, trigger the module builds, merge ------------------------
    run_factory = util.BuildFactory()
    run_factory.addStep(ShellCommand(
        name = 'remove old checkouts', workdir = '.',
        command = runner('sweep', '--base', g.workers_dir, '--buildbot-url', g.internal_url),
        env = env, usePTY = False, haltOnFailure = False, flunkOnFailure = False, warnOnFailure = False))
    run_factory.addStep(SetPropertyFromCommand(
        name = 'git fetch origin', workdir = '.',
        command = runner('sync', '--branch', BRANCH, '--trigger', trigger_name),
        extract_fn = extract_sync, env = env, haltOnFailure = True))
    run_factory.addStep(ShellCommand(
        name = 'checkout', workdir = '.',
        command = runner('checkout', '--dest', run_tree, '--revision', REVISION),
        env = env, usePTY = False, doStepIf = wanted, haltOnFailure = True))
    run_factory.addStep(SetPropertyFromCommand(
        name = 'plan', workdir = RUN_DIR,
        command = runner(
            'plan',
            '--branch', BRANCH,
            '--tests', tests_arg,
            '--run-id', util.Interpolate('%(prop:run_id:-)s'),
            '--requested-by', requested_by_arg,
            '--reason', reason_arg,
            '--trigger', trigger_name,
            '--build-number', util.Interpolate('%(prop:buildnumber)s'),
            '--build-url', util.URLForBuild,
            tree = checkout),
        extract_fn = extract_plan, env = env, doStepIf = wanted, haltOnFailure = True))
    run_factory.addStep(ShardTrigger(
        name = 'modules',
        schedulerNames = [repo.shard_scheduler(1)],
        waitForFinish = True,
        set_properties = {'run_id': util.Property('plan_run_id'), 'tested_revision': util.Property('tested_revision')},
        doStepIf = has_shards,
        hideStepIf = lambda results, step: not has_shards(step),
        flunkOnFailure = False, warnOnFailure = False, haltOnFailure = False))
    run_factory.addStep(ShellCommand(
        name = 'finalize', workdir = RUN_DIR,
        command = runner('finalize', '--run-id', util.Interpolate('%(prop:plan_run_id:-%(prop:run_id:-)s)s'), tree = checkout),
        env = env, usePTY = False, alwaysRun = True, doStepIf = has_run, haltOnFailure = False))
    run_factory.addStep(ShellCommand(
        name = 'release checkout', workdir = '.',
        command = runner('release', '--dest', run_tree),
        env = env, usePTY = False, alwaysRun = True, doStepIf = wanted, flunkOnFailure = False, warnOnFailure = False))
    run_factory.addStep(steps.RemoveDirectory(
        name = 'clean up', dir = RUN_DIR, alwaysRun = True, doStepIf = wanted, flunkOnFailure = False, warnOnFailure = False))

    # ---- the build of one module of a run, on one worker ------------------------------------------------------
    shard_factory = util.BuildFactory()
    shard_factory.addStep(ShellCommand(
        name = 'checkout', workdir = '.',
        command = runner('checkout', '--dest', checkout, '--revision', REVISION),
        env = env, usePTY = False, haltOnFailure = True))
    if adapter.has_prepare(repo.options):
        shard_factory.addStep(ShellCommand(
            name = 'prepare', workdir = '.',
            command = runner('prepare', tree = checkout),
            env = env, usePTY = False, haltOnFailure = True))
    shard_factory.addStep(ShellCommand(
        name = 'module', workdir = '.',
        command = runner(
            'shard',
            '--run-id', util.Interpolate('%(prop:run_id)s'),
            '--shard-index', util.Interpolate('%(prop:shard_index)s'),
            '--worker', util.Interpolate('%(prop:workername)s'),
            '--build-number', util.Interpolate('%(prop:buildnumber)s'),
            '--build-url', util.URLForBuild,
            tree = checkout),
        env = env, usePTY = False, sigtermTime = 30,
        timeout = repo.step_timeout, maxTime = repo.step_max_time,
        haltOnFailure = False, descriptionDone = 'module tests'))
    shard_factory.addStep(ShellCommand(
        name = 'release checkout', workdir = '.',
        command = runner('release', '--dest', checkout),
        env = env, usePTY = False, alwaysRun = True, flunkOnFailure = False, warnOnFailure = False))

    # ---- reading the test list of a branch ----------------------------------------------------------------
    catalog_factory = util.BuildFactory()
    catalog_factory.addStep(ShellCommand(
        name = 'test list', workdir = '.',
        command = runner('catalog', '--branch', BRANCH),
        env = env, usePTY = False, timeout = 1800))

    branches = repo.branch_names

    def branch_form(label):
        return util.CodebaseParameter(
            '', label = 'Source',
            branch = util.ChoiceStringParameter(name = 'branch', label = label, choices = branches, default = branches[0]),
            revision = util.FixedParameter(name = 'revision', default = ''),
            repository = util.FixedParameter(name = 'repository', default = ''),
            project = util.FixedParameter(name = 'project', default = ''))

    def fixed_branch(branch):
        return util.CodebaseParameter(
            '', label = 'Source',
            branch = util.FixedParameter(name = 'branch', default = branch),
            revision = util.FixedParameter(name = 'revision', default = ''),
            repository = util.FixedParameter(name = 'repository', default = ''),
            project = util.FixedParameter(name = 'project', default = ''))

    # Every branch has its own nightly scheduler (its own time, always every test) and its own force scheduler
    # (every test or a selection, on request).
    nightly = [
        schedulers.Nightly(
            name = repo.nightly_scheduler(branch), branch = branch,
            codebases = {'': {'repository': '', 'branch': branch, 'revision': None}},
            minute = cron_value(repo.branch_cron(branch)[1]), hour = cron_value(repo.branch_cron(branch)[0]),
            builderNames = [repo.run_builder]) for branch in repo.scheduled_branches
    ]
    force = [
        schedulers.ForceScheduler(
            name = repo.force_scheduler_for(branch), builderNames = [repo.run_builder],
            buttonName = 'Run %s tests on %s' % (repo.name, branch),
            codebases = [fixed_branch(branch)],
            properties = [
                util.StringParameter(name = 'tests', label = 'Tests (modules, identifiers or globs; empty = all tests)', default = '', size = 80),
                util.StringParameter(name = 'run_id', label = 'Run identifier (set by the website)', default = ''),
                util.StringParameter(name = 'requested_by', label = 'Requested by', default = ''),
            ]) for branch in branches
    ]
    all_schedulers = nightly + force + [
        schedulers.ForceScheduler(
            name = repo.catalog_scheduler, builderNames = [repo.catalog_builder],
            buttonName = 'Refresh %s test list' % repo.name, codebases = [branch_form('Branch')]),
    ] + [
        schedulers.Triggerable(
            name = repo.shard_scheduler(index), builderNames = [repo.shard_builder],
            properties = {'shard_index': str(index)}) for index in range(1, repo.max_shards + 1)
    ]

    parallel = min(repo.max_parallel, len(repo.workers)) if repo.max_parallel else 0
    all_builders = [
        util.BuilderConfig(name = repo.run_builder, workernames = repo.workers, factory = run_factory,
                           locks = [util.MasterLock('%s-runs' % repo.key, maxCount = repo.max_runs).access('counting')]),
        util.BuilderConfig(name = repo.shard_builder, workernames = repo.workers, factory = shard_factory,
                           locks = [util.WorkerLock('%s-shard' % repo.key).access('exclusive')]
                                   + ([util.MasterLock('%s-shards' % repo.key, maxCount = parallel).access('counting')] if parallel else [])),
        util.BuilderConfig(name = repo.catalog_builder, workernames = repo.workers, factory = catalog_factory),
    ]
    all_workers = [worker.Worker(name, g.worker_password, max_builds = repo.max_runs + 1) for name in repo.workers]
    return all_workers, all_schedulers, all_builders


def idle_config(g):
    """Buildbot refuses to start without a builder and a worker. Until the first repo profile is added in the web UI,
    this stand-in keeps the master running (its worker never connects)."""
    factory = util.BuildFactory()
    factory.addStep(steps.ShellCommand(name = 'nothing', command = ['true']))
    return ([worker.Worker('ci-idle', g.worker_password)],
            [schedulers.ForceScheduler(name = 'force-idle', builderNames = ['ci-idle'], buttonName = 'No repository profile yet')],
            [util.BuilderConfig(name = 'ci-idle', workernames = ['ci-idle'], factory = factory)])
