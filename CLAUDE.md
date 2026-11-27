# CLAUDE.md

Detailed reference for this repository: architecture, contracts, settings, scripts, data layout. `README.md` has only what the project is and how to set it up locally and on a server.

## Conventions

- Never hardcode a home folder in code or scripts: take paths from `HOME` (`os.path.expanduser`, `$HOME` in `bin/`) or from the `CI_*` settings. The only fixed paths are in `deploy/systemd/*.service` (systemd does not expand `$HOME`, and `%h` is `/root` in system units); they name the account to edit on install.
- `~/bb-master.env` is the one settings file for the CI as a whole. Read the format rules under "Settings files" before editing it. Nothing specific to one repository goes into code or into that file: it belongs in a repo profile (see "Repo profiles").
- A new kind of test suite is a new adapter (see "Adapters"). Nothing outside `hub/adapters/` names a particular suite type.
- Write words in full in everything people read (pages, buttons, dialogs, messages, help texts): "repository", not "repo"; "identifier", not "id"; "differences", not "diff"; "minutes", not "min"; "website", not "UI"; no initials as a logo (the logo is an icon). Code identifiers, addresses and command lines may stay short (`repo_dir`, `/r/<key>/`, `--repo`). `ciweb/repos/tests/test_branches.py: WritingRulesTest` checks the pages for this. The word "regression" belongs only to the Output comparison type, where it names the Indian Kanoon regression framework.
- Style sheet: no pixel values anywhere (`rem`, `em`, `%`, `vw`, `clamp()`), also in media queries, so the site follows the reader's text size and fits a phone, a tablet, a laptop and a large screen. A test checks it. Colours and sizes are variables at the top of `app.css`.
- Scripts in `bin/` load `~/bb-master.env` themselves (`CI_ENV` points them at another file) and use `CI_UI_VENV` (default `$HOME/.buildbotci`) for the Python environment.
- The tests of this repository must pass before a change is done (commands in "Tests").

## Overview

The CI runs the test suites of several repositories on a schedule and on demand. A small website lets developers pick a repository, then a branch, pick tests, start runs, watch them, read logs and results, and, for suites that compare output with an expected file, see why a test failed. Administrators add and change repositories in the same website: nothing about a particular repository is in the code or in the settings file.

This is a separate repository. The tested repositories (`indianlaw`, `egazette`, `legal-chatbot`, ...) contain no CI code and never refer to this one. For each *repo profile* the CI reads a git clone you name (its remote is called `origin`): there is no webhook and nothing is cloned; the CI runs `git fetch origin` in that clone.

```
~/
  buildbotci/     this repository (the CI)
  indianlaw/      clone of a tested repository (one per repo profile; any folder)
  egazette/
```

```
                            repo profiles (website, database)
                                       |  writes profiles.json
every night ─────────────┐             v                          ┌─► worker indianlaw-w1: module A ─┐
                         ├─► Buildbot master ───────────────────┼─► worker indianlaw-w2: module B ─┼─► $CI_RESULTS_DIR/indianlaw
developer, website ──────┘   (builders per repo)                  └─► worker indianlaw-w3: module C ─┘          ▲
Website (ciweb, Django) ───────────────── reads run directories ─────────────────────────────────────────────────┘
```

Each module of a run goes to its own worker of that repository. A run with 5 modules and 3 workers runs 3 modules at a time and queues the other 2 for whichever worker frees up first. A worker never runs two modules at once, and a worker serves one repository only.

| Part | Where |
|---|---|
| Repo profile (the description of a repository; shared by master, runner and website) | `hub/repos.py` |
| Test suite types ("adapters") | `hub/adapters/` (`base.py`, `comparison.py`, `pytest_suite.py`, `command.py`, `process.py`) |
| Runner commands (`sync`, `checkout`, `release`, `plan`, `shard`, `finalize`, `catalog`, `run`, `select`, `sweep`, `prune`, `prepare`, `repos`) | `runner.py` → `hub/cli.py` → `hub/commands/` |
| Running and recording one test | `hub/execution.py` |
| Which tests a run covers | `hub/planning.py`, `hub/selection.py`, `hub/impact_map.json` |
| Git clone, checkouts, sweeping | `hub/gitrepo.py` |
| Run directory format | `hub/store.py` |
| Differences between expected and actual output | `hub/diffing.py`, `hub/diffparse.py` |
| Buildbot master configuration | `hub/buildbot/master.cfg` (thin), `hub/buildbot/repo_config.py` (one repository's builders), `hub/buildbot/watcher.py`, `hub/buildbot/cleanup.py` |
| Website (Django project) | `ciweb/`: `ciweb/repos/` (repo profiles: model, form, settings pages, clean-up, worker control), `ciweb/runs/` (results pages), `ciweb/accounts/` (people, sign-in, administration), `manage.py` |
| Scripts | `bin/` (see "Scripts") |
| Server templates | `deploy/` (settings example, systemd units, nginx) |
| Tests for all of the above | `hub/tests/`, `ciweb/*/tests/` |

## Repo profiles

A repo profile (`hub/repos.py: RepoConfig`, stored by `ciweb/repos/models.py: RepoProfile` and its `Branch` rows) holds everything specific to one repository:

| Setting | Meaning |
|---|---|
| `key`, `name`, `description`, `enabled` | the key is the short name used in addresses, folders and Buildbot names (`indianlaw`); it cannot change. A disabled profile keeps its results but is not tested, cannot be run, and is not offered on the start page |
| `repo_dir`, `checkout_name` | the git clone (remote `origin`), and the folder name of a test checkout (default: the name of the clone folder). `indianlaw` code imports itself by name, so its checkout must be called `indianlaw` |
| `python`, `env` | the interpreter of the repository's own environment (written as `$HOME/.indianlaw/bin/python`, `~/...` or an absolute path; `$HOME` and `~` are expanded when the profile loads, because Buildbot and `subprocess` start it without a shell; a bare name such as `python3` is looked up on `PATH`), and extra environment variables the tests run with. The runner itself is started with this Python |
| `adapter`, `options` | the test suite type and its settings (see "Adapters") |
| branches | the branches people can pick. Each has a flag "test automatically" (nightly run) and optionally its own hour and minute (empty: the repository's `cron_hour`/`cron_minute`). **Every branch gets two schedulers**: `<key>-nightly-<branch>` (all tests, only when there are new commits, at the branch's own time) and `force-<key>-<branch>` (on request: all tests or a selection). A branch name is written into scheduler names with every character other than letters, digits, `-` and `_` as `_` (`release/1.0` is `release_1_0`; two branches that give the same name are refused) |
| `schedule`, `cron_hour`, `cron_minute` | when the flagged branches are checked; with no new commit the check does nothing |
| `workers` (count or names) | the Buildbot workers of this repository, `<key>-w1`, `<key>-w2`, ... by default; a worker serves one repository only |
| `max_runs`, `max_shards`, `max_parallel`, `step_timeout`, `step_max_time` | limits |

**How a change reaches Buildbot.** Saving a profile in the website writes `profiles.json` (default `$CI_RESULTS_DIR/profiles.json`, mode 600, atomic, unchanged content is not rewritten) and the master follows it by itself:

1. `hub/buildbot/master.cfg` reads the snapshot and, for every enabled profile, `repo_config.build_repo` makes its workers; its nightly (one per automatic branch, at its own time), force (one per branch), catalog and shard schedulers; and its three builders `<key>-run`, `<key>-shard`, `<key>-catalog`. The names come from the `RepoConfig` properties, so the website, the runner and the master always agree. With no profile, a stand-in builder keeps the master running.
2. `watcher.ProfilesWatcher` (a Buildbot service) compares the file with what it loaded every 10 seconds and does the same reconfiguration as `bin/master reconfig` (running builds are kept), then cleans up what Buildbot still holds for anything that was removed (below). It reports in `profiles.applied.json` which version it loaded or why it could not; the repository pages show that as "Buildbot is using these settings", "loads the change within seconds" or "could not load".
3. The runner (on the workers) and the website read the same file. It lives in the results folder because every worker and the website must see that folder anyway.

Everything the master needs from `hub/` is reloaded on each reconfiguration, so changes to `hub/repos.py`, the adapters (their option declarations) and `repo_config.py` apply with `bin/master reconfig`. A profile that cannot be loaded (for example a worker used by two repositories) is rejected as a whole: the master keeps the old configuration and the page shows the error.

### Deleting, disabling and changing a repository: the clean-up

Taking something out of the profiles only removes it from the *configuration*. Buildbot would keep its rows (inactive builders, schedulers and workers) and its waiting build requests for ever, and the worker processes would keep trying to log in. Two parts clean up.

**In the master (`hub/buildbot/cleanup.py: reconcile`)**: it runs after every reconfiguration the watcher does, and once when the master starts, so it also covers a change made while the master was stopped. It makes Buildbot's database match the loaded configuration:

- waiting build requests of builders that are no longer configured are cancelled and their buildsets completed;
- running builds of such builders are stopped;
- schedulers that are no longer configured are deleted (a dropped branch, a lower `max_shards`, a deleted repository);
- workers that are no longer configured, not connected and without builds are deleted;
- builders that are no longer configured and have no builds are deleted. A builder with builds keeps them, so old runs stay readable in Buildbot, unless the website asked for its history to be erased (it writes the builder names to `cleanup.json` next to `profiles.json`; the master erases them once they are unconfigured and removes them from the file). Deleting a builder takes its builds, steps, logs and requests with it (`delete_cascade`, which follows `ON DELETE CASCADE` links only).

**In the website (`ciweb/repos/decommission.py`)**: **Manage repositories → the repository → Delete…** opens a page that shows what will happen and offers three options: stop and delete the worker folders on this machine (default on), delete the results of the repository (off: results are kept), erase Buildbot's history of the repository (off). It then, in this order:

1. cancels the waiting requests and stops the running builds of the repository's builders, and waits up to 30 seconds for their last steps (`finalize`, `release checkout`) to run;
2. closes the run records that are still open as `aborted`;
3. deletes the profile (the master then drops the rest);
4. stops each worker with `buildbot-worker stop` and deletes its folder and checkouts (only folders named like the repository's workers, directly inside `CI_WORKERS_DIR`);
5. deletes `<results>/<key>` if asked.

If Buildbot cannot be reached the delete still happens and says so. Workers under systemd need `systemctl disable --now buildbot-worker@<name>` (the page prints it); workers on other machines must be stopped there. **Disable** stops the repository's queued and running runs as well (after a confirmation) and keeps everything else; **Enable** brings it back. The same from a shell: `python manage.py ci_repos remove KEY` shows what would happen, `--yes` does it (`--keep-workers`, `--erase-results`, `--erase-history`). To remove only some workers and keep the repository, see "Deleting some workers".

## How the CI reads a repository

The CI only watches the branches of the repo profile, and only through `origin`:

- Every run starts with `git fetch origin` in the profile's clone, and takes the latest commit of `origin/<branch>`.
- It **never** switches branches, resets files, or writes into your working copy. Each run, and each worker, gets its own throwaway checkout made with `git worktree` from that clone. They share its objects, so nothing is cloned again.
- A run's checkout is removed when the run ends, and so is a module build's checkout (the last step of each build, `release checkout`, always runs, also after a stop or an abort). Checkouts that a crash or a kill left behind are removed too: every run starts with the step `remove old checkouts` (`runner.py sweep`), which asks Buildbot which builds are running and removes every other checkout under the worker folders (`CI_WORKERS_DIR`), whether git still lists it or not. If Buildbot is not running to do this, use `bin/clean-worktrees` (`--dry-run` only lists; `--all` when Buildbot is stopped). `bin/clear-queue --yes` runs the same cleanup at its end. Checkouts of the repository that are not under a worker's `<key>-run` or `<key>-shard` folder, for example your own, are never touched.
- Git commands on a clone are serialised with a lock file (`.git/ci.lock`), so concurrent runs cannot collide.
- On a machine with several workers, each machine needs this repository and the clones at the same paths as on the master.

It is safe to point a profile at the clone you develop in. Do not delete worker checkouts by hand: use `git worktree prune` in that clone if you ever do.

## Adapters: test suite types

An adapter (`hub/adapters/base.py: Adapter`) is the only code that knows how one kind of suite lists and judges its tests. Everything else (git, Buildbot, sharding, run directories, pages) is the same for all. An adapter provides:

| Member | Meaning |
|---|---|
| `kind`, `label`, `summary`, `fields` | identity and its settings; the repository form is built from `fields` (`Field(name, label, kind, default, help, required, choices)`; kinds `text path lines bool int choice`), so a new type needs no form code |
| `catalog()` | `{'modules': {...}, 'tests': [{'id', 'module', 'name', 'doc', 'output', 'has_expected'}]}`; an identifier is `<module>.<name>` and a valid folder name. Modules are the unit of parallel work |
| `open()` | what is loaded once per process (a `Session`; its `error` makes every test report it) |
| `run_test(session, entry, scratch)` | runs one test, returns an `Outcome(status, reason, actual, expected, diff, stats, hunks, extra)`. Printing goes to the test's log; an exception is the status `error` |
| `reproduce(entry)`, `baseline_note(entry)` | how to run it by hand; how to create the missing baseline (shown on the test page) |
| `activate()`, `prepare_checkout()`, `has_prepare(options)` | making a checkout importable; an optional build step after the checkout |
| `impact_map_path()`, `diff_rules()` | optional: map from changed files to tests; record rules that make differences readable |

`hub/execution.py: execute_test` wraps every test the same way: log capture into `log.txt`, `result.json`, and keeping `actual`, `expected` and `diff.txt` for a test that did not pass.

The three types:

- **Output comparison** (key `comparison`; for the Indian Kanoon regression framework): the contract below. Settings: environment profile (`-P`), optional machine settings file (merged into `.env.<profile>` by the step `prepare`; only listed settings change and the log names them, never their values; needed because the code loads `.env.<profile>` with override), log level. Tests run inside the runner process, in the repository's Python.
- **pytest (Python tests)** (key `pytest`): `pytest --collect-only` lists tests; a module is a test file; each test runs as its own `pytest <nodeid>` process with a time limit, and the result is pytest's exit code and the first failure message of its JUnit report. Settings: paths, extra options, folder, time limit.
- **Command checks** (key `command`): each check is a line `name | command` (a name like `smoke.imports` puts it in the module `smoke`). It passes on the expected exit code; in *golden* mode its output must also equal `<golden folder>/<name>.txt`, and a difference is shown like an output comparison (a missing file is `no_expected`). Commands run in the checkout with the repository's Python first on `PATH`.

**Names.** A test suite type has a *key* (stored in the database and in `profiles.json`), a *label* (what people read) and an adapter class named after the key: key `comparison`, label **Output comparison**, `OutputComparisonAdapter`; key `pytest`, label **pytest (Python tests)**, `PytestAdapter`; key `command`, label **Command checks**, `CommandAdapter`. Name a type after what it does, never after a repository. A renamed type keeps its old key working through `ADAPTER_ALIASES` in `hub/repos.py` (the type `comparison` was once called `indianlaw`; the database was migrated, and old `profiles.json` files and run records are still read).

### Adding a new type of test suite

1. Subclass `Adapter` in `hub/adapters/<name>.py` (standard library only at import time: the master imports it too; import heavy things inside methods).
2. Add it to `ADAPTERS` in `hub/adapters/__init__.py`, and its module to the reload list in `master.cfg`.
3. Add its tests in `hub/tests/test_adapters.py`. The form, the pages and Buildbot need no change.

If an existing type almost fits, prefer adding a setting to its `fields`.

### The contract of Output comparison with its repository

The adapter imports `regressions/regression.py` from a checkout of `indianlaw`. It needs these names, and nothing else, from it:

| Name | Meaning |
|---|---|
| `MODULES` | dict of module name to `(class, factory)`; `factory(storage, datasrcs)` builds the object |
| `get_class_tests(cls)` | names of the test methods of a class |
| `output_filename(test)` | name of the expected file of a test |
| `compare_files(expected, actual, labels)` | `(status, unified diff)`; the status decides pass or fail. For a failed test the runner replaces the diff text with `git diff --no-index --histogram` of the same two files (the worker needs `git`; if git cannot run, the diff from `compare_files` is kept) |
| `load_regress_objs(storage, datasrcs, modules)` | objects for the given module names |
| `PASSED`, `FAILED`, `ERROR`, `NO_EXPECTED` | status strings |

It also imports `indianlaw.setup.data_srcs.DataSrcFactory`, `indianlaw.utils.commonfuncs.setup_logging` and `indianlaw.storage_manager.StorageManager`, and reads `regressions/expected/`. A branch without `MODULES` in its `regression.py` cannot be tested. The checkout folder must be named `indianlaw` (the profile's checkout name), because the code imports `indianlaw.*`. `hub/tests/test_contract.py` checks all of this against a real checkout (`CI_REPO_DIR`, default `../indianlaw`); run it after every change to `regression.py`. It is skipped when there is no checkout.

## Runs

### On a schedule

1. At each scheduled time of a repo profile (for example every night at 21:00 in the master's `TZ`), the master starts one `<key>-run` build per branch that is marked "test automatically".
2. The first step runs `git fetch origin` and compares `origin/<branch>` with the last commit that was scheduled for that branch (`<key>/state/last_tested.json`). If it is the same commit, the build ends there with "No new commits. Nothing to run." Otherwise the commit is recorded and the run goes on. The first scheduled check after a fresh install always runs, since nothing was recorded yet.
3. A scheduled run tests **all modules** of that commit and is recorded as requested by `Auto`.
4. The build makes a private checkout of that commit and runs `runner.py plan`, which groups the tests by module. Each module becomes one shard.
5. The build triggers one `<key>-shard` build per module. Buildbot assigns each to a free worker of that repository, which updates its own checkout to the same commit, runs the suite type's `prepare` step if it has one, and runs `runner.py shard` for that module only. Each test runs in isolation, and failures never stop the shard. Each shard writes its own progress file, so the website shows live progress per worker.
6. When every shard has ended, the `finalize` step (it always runs) merges the shards into the final run result. A shard that never reported, because its worker was lost or cancelled, marks its tests `aborted` and the run `aborted`.
7. `finalize` exits 1 if any test is not `passed`, so the Buildbot build goes red. The private checkout of the run is removed.

### On demand

A developer signs in to the website, opens a repository, clicks one of its branches (the branch cards show the latest result of each; the run list below follows the branch you clicked), then **Run tests**, picks the modules or tests, and clicks **Queue run**. The same steps follow, except: `git fetch origin` is always followed by a run (it never skips), only the selected tests run, and the run is recorded under the developer's name. The reason shown in Buildbot starts with that name.

The test list in the picker belongs to the branch you chose, and is read from `origin/<branch>` by Buildbot (`catalog`). Every run refreshes the list of its branch. The **Refresh test list** button refreshes it without running anything.

### Choosing tests

- **Scheduled runs**: every module.
- **Manual runs**: in the website, tick modules or single tests, or pick everything. Module names (`header`), test identifiers (`header.bombay_hdr`), globs (`*.aft_*`) and `all` are all accepted, which is also what the Buildbot "Run <repo> tests" form takes in its Tests box. An empty selection means all.

The impact map (`hub/selection.py`, `hub/impact_map.json`) is an `indianlaw` feature that Buildbot and the website do not use. It serves the command line only: `python runner.py --repo indianlaw select --base <rev>` previews which tests changed files would map to, and `runner.py run` with no arguments uses it. Suite types without a map run in full.

### Stopping a run

**Who may stop a run.** The person who started it, and administrators (accounts marked staff or superuser). Nobody else: the button is not shown to them, and the address refuses them. When an administrator stops somebody else's run, or an automatic (`Auto`) one, the button leads to a page that names whose run it is, how far it got, and asks for confirmation (`runs/stop_confirm.html`); the stop only happens after "Yes, stop ...", and the server insists on that confirmation too. The website does this check itself (`ciweb/runs/views.py: stop_rights`), because Buildbot sees only the website's one shared account (`CI_BUILDBOT_USER`, role `forcers`).

**What happens.** The website asks Buildbot, with that account, to cancel the run's queued requests and stop its running builds: the run's own build (`<key>-run`) and every module build (`<key>-shard`). Cancelling a queued request is sent to Buildbot as `cancel`, which Buildbot's stock stop rule does not cover (it would demand the `admins` role), so `master.cfg` has `StopOrCancelMatcher` for it. Each worker then sends SIGTERM to its runner, which gives the test a moment, marks the unfinished tests `aborted`, and the `finalize` step writes the final result. The run ends as `aborted` with the message "Stopped by <user>", usually within seconds. A run that was still waiting in the queue is closed by the page itself. Tests that already finished keep their result. The request is kept in `stop.json` in the run directory.

### Runs overlap

Runs do not wait for each other. Each run has its own checkout and run directory, and a worker handles one module at a time. The repo profile's "runs at once" (default 8) caps how many runs of that repository are in progress at once, and an extra run waits. Repositories do not share workers, so a long run of one repository never delays another.

What is **not** isolated: the databases. Two runs at the same time can run the same module against the same databases. If a module writes shared data (for example, in `indianlaw`, `header` and `layout` can write TIDDB), two overlapping runs can disturb each other's results. Check that before relying on overlapping runs.

### Test statuses

| Status | Meaning |
|---|---|
| `passed` | The suite type says so: for Output comparison, output identical to `regressions/expected/<name>` (or a non-output test returned truthy); for `pytest`, pytest passed it; for `command`, the exit code (and, in golden mode, the output) is as expected |
| `failed` | Output differs, the exit code or the pytest result is wrong, or a non-output test returned falsy |
| `no_expected` | The test ran but its expected file (`expected/<name>`, or the golden file) does not exist. This is a failure |
| `error` | The test, or its module's setup, raised. Traceback, partial output and log are kept |
| `aborted` | Buildbot cancelled or timed out the run (the runner gets SIGTERM first) |

## Scripts

All scripts are in `bin/`, load the settings file themselves and build every path from `$HOME`. The ones that change something ask for a website superuser first ("Who may run the scripts"). Those that act on runs, checkouts or workers take a repository key (`KEY`) or `--repo KEY`; without one they cover every repository.

| Script | Does |
|---|---|
| `bin/master update` | **The way to apply `master.cfg`.** Copies `hub/buildbot/master.cfg` into the master folder, checks it, then starts the master, or reloads it when it already runs (running builds are kept). Stops before touching anything if the check fails |
| `bin/master check\|start\|stop\|restart\|reconfig\|status` | `check` only runs `buildbot checkconfig` on what is installed; `reconfig` reloads without copying (for changes to `hub/repos.py`, `hub/adapters/`, `hub/buildbot/repo_config.py`); `status` exits 3 when the master is stopped |
| `bin/create-workers [KEY]` | Makes the worker folders that do not exist yet (the website does this by itself when a repository is saved; use it for another machine, or after a problem) |
| `bin/workers start\|stop\|restart\|status [KEY] [--worker NAME]` | Controls the workers on this machine |
| `bin/delete-workers [KEY] [--worker NAME] [--orphans]` | Deletes chosen workers; the repository stays |
| `bin/run-ui` | Migrates, collects static files, starts gunicorn (reads `CI_UI_BIND`, default `127.0.0.1:8020`; `CI_UI_WORKERS`, default 2; `CI_UI_PID_FILE`, default `$CI_UI_VENV/ui.pid`) |
| `bin/apply-update` | Installs `requirements.txt`, migrates, collects static files, reloads the website |
| `bin/login`, `bin/logout` | log in once for this terminal so the scripts that change something do not ask again; end it. `bin/login --who` |
| `bin/_login` | not a script to run: the part the others share to ask for a website superuser (see "Who may run the scripts") |
| `bin/wait-for-db [SECONDS]` | Waits until the database accepts logins (default 90 seconds, or `CI_DB_WAIT_SECONDS`); never prints the password |
| `bin/prune-runs --days N` | Lists runs older than N days; `--yes` deletes them |
| `bin/clean-worktrees` | Removes leftover checkouts (`--dry-run`, `--all`) |
| `bin/clear-queue` | Shows unfinished work the master would resume after a crash; `--yes` cancels it. Stop the master first |

`bin/master` takes `--systemd` or `--direct`, and so does `bin/workers`: by default they use systemd when the unit (`buildbot-master.service`, `buildbot-worker@.service`) is installed, and the `buildbot` or `buildbot-worker` command on the folder otherwise. A refused `systemctl` prints the `sudo systemctl ...` line to run. The master folder is `$CI_MASTER_DIR` (default `$CI_UI_VENV/master`).

### Who may run the scripts: website superusers

A script in `bin/` that **changes something** first asks for the user name and password of a website superuser and checks them against the accounts of the website (the `ciweb` schema), with the same rules as signing in to the website (`ciweb/accounts/superuser.py`): name or email in any letter case, five wrong passwords lock the account for 15 minutes, and the reasons "waiting for approval", "disabled" and "not a superuser" are only given after the right password. Being staff is not enough: the account must be active **and a superuser**. A refused login stops the script before it does anything. Every use, allowed or not, is one line in `$CI_RESULTS_DIR/admin-actions.log` (time, user, script and action, result; never a password).

| Asks for a superuser | Asks nothing |
|---|---|
| `bin/master` `update`, `start`, `stop`, `restart`, `reconfig` | `bin/master` `check`, `status` |
| `bin/workers` `start`, `stop`, `restart` | `bin/workers status`, and any `--dry-run` |
| `bin/create-workers` | `bin/delete-workers`, `bin/prune-runs`, `bin/clear-queue` without `--yes` (they only list) |
| `bin/delete-workers`, `bin/prune-runs`, `bin/clear-queue` with `--yes` | `bin/clean-worktrees --dry-run` |
| `bin/clean-worktrees`, `bin/apply-update` | `bin/run-ui`, `bin/wait-for-db`: systemd runs them with nobody there, so the unit is what authorises them |

- **One login for many scripts.** The first script that asks, or `bin/login` before them, logs you in for **this terminal**: the scripts that follow (`bin/master update`, then `bin/workers start`, ...) do not ask again. The login ends after `CI_LOGIN_MINUTES` of not being used (default 30; each script run renews it; `0` asks every time), 8 hours after it began however often it is used, when the terminal is closed, and when the account is disabled, loses its superuser right or changes its password (checked on every use). `bin/logout` ends it by hand; `bin/login --who` says who is logged in. Every script run is still logged to `admin-actions.log`, as "allowed (login of this terminal)". The login is a small file signed with the website's secret (`ciweb/accounts/loginsession.py`), one for each terminal, in `$XDG_RUNTIME_DIR` (cleared at logout and reboot) or `~/.cache/ci`, mode 600; another terminal, or another account, has its own. A job with `CI_ADMIN_USER` and `CI_ADMIN_PASSWORD` keeps no login.
- **Without a terminal** (cron, a deploy job): set `CI_ADMIN_USER` and `CI_ADMIN_PASSWORD` for that job. Without a terminal and without them, nobody is let in. A password in a cron environment is only as safe as the file that holds it: keep it mode 600.
- **Before the first superuser exists** (a fresh install, before `createsuperuser`, or before the tables exist) nothing is asked, and the log says so. That is what lets the local setup run `bin/master update` before the website has accounts.
- **If the database cannot be read** nobody is let in. The programs behind the scripts (`buildbot stop`, `buildbot-worker stop`, ...) can still be run directly, for example to stop the master while the database is down.
- **What this is, and is not.** It makes sure the person at the keyboard is a known superuser and leaves a record of who did what. It is not a wall against the operating system account: whoever can read the settings file (`bin/` scripts need it, and it holds the database password) can run the underlying commands or `manage.py` directly, as the "Accounts" section says. Keep the settings file at mode 600, owned by the CI account.
- `bin/_login` is the shared part (sourced by the scripts, not run by itself): `ci_login SCRIPT ACTION` asks, `ci_has --yes "$@"` tests for a flag. `manage.py ci_login` is what it runs. A new script that changes something sources `_login` and calls `ci_login` before it does; `hub/tests/test_bin_login.py` lists, for every script, whether it asks.

### Starting and stopping workers

`bin/workers status` lists every worker folder on this machine (running or stopped, busy with a build or not). Which workers: `KEY` (all of that repository's), `--worker NAME` (repeat for several; it must belong to `KEY` when both are given), or neither (every worker folder here, also those no repository lists). `--dry-run` only says what would happen. Examples: `bin/workers start`, `bin/workers stop egazette`, `bin/workers restart --worker egazette-w2`.

Under systemd it runs `systemctl <action> buildbot-worker@<name>`; directly it runs `buildbot-worker start|stop|restart <folder>` and finds out whether a worker runs from `twistd.pid`. A direct start inherits the environment of the script. A worker that is already in the wanted state is reported and skipped. **A worker that is running a build is not stopped or restarted** (it would end that module's build and the run would mark its tests `aborted`): Buildbot is asked first, and when it cannot be reached a running worker is left alone; `--force` overrides both. The script exits with an error if any worker could not be handled, including a folder that does not exist (`bin/create-workers` makes it). Starting does not enable the unit at boot (`systemctl enable --now` does). Workers on other machines are handled there.

### Workers are made and started when a repository is saved

When an administrator saves an enabled repository in the website, the website (1) makes the worker folders that do not exist yet on **its own machine** and (2) starts the workers that are not running (`ciweb/repos/workerctl.py: create_and_start`; the same code as `bin/create-workers` and `bin/workers start`). It tells the administrator what it made and started. Saving again, or raising the number of workers, handles only what is missing or not running. A problem (for example `BUILDBOT_WORKER_PASSWORD` not set in the website's settings file, or a refused `systemctl`) is a warning on the page and never undoes the save. A disabled repository gets nothing.

For a repository that already exists, the **Workers** card on its page has a **Create and start workers** button (administrators only; shown while a worker is not created or not connected; it works even when the automatic switches are off). Starting follows the same choice as `bin/workers`: under systemd the website runs `systemctl start buildbot-worker@NAME`, which a non-root account is usually refused, and the page then prints the `sudo systemctl ...` line to run; without systemd it runs `buildbot-worker start <folder>`.

**A worker started by the website or by `bin/workers` gets a clean environment** (`workerctl.clean_environment`: `PATH`, `HOME`, `USER`, `LANG`, `LC_*`, `TZ`, ... and nothing else). The settings file holds the database password and the website secret, and the tests of the repositories run on the workers, so none of it is passed on: the master gives each build step the few settings it needs (`CI_RESULTS_DIR`, `CI_PROFILES_FILE`, `CI_UI_URL`), like the systemd unit, which does not load the settings file either.

Switches: `CI_AUTO_CREATE_WORKERS=0` and `CI_AUTO_START_WORKERS=0` stop the automatic part (the button still works). If the workers run on another machine than the website, switch both off: the folders and processes belong there (`bin/create-workers`, `bin/workers start` on that machine).

### Seeing which workers are connected

Whether a worker is *connected to the master* is Buildbot's knowledge, not the website's, so the pages ask the master (`ciweb/runs/buildbot_client.py: worker_states`, which reads Buildbot's workers and running builds). The repository page (**Manage repositories → the repository**) has a **Workers** card with one row for each worker: **Connected**, **Busy** (running builds), **Not connected** (the folder exists here but the worker is not running or cannot log in: `bin/workers start --worker NAME`), **Not created** (no folder here and not connected: `bin/create-workers KEY`) or **Unknown** (Buildbot could not be asked). A worker on another machine counts as connected without a folder here. The repository list shows a chip for each worker and "2 of 3 connected". `bin/workers status` prints the same for this machine, and for a running worker the master does not see it names the file to read (`<worker folder>/twistd.log`). The card appears once the repository is saved: the add page has no workers to show yet.

### Deleting some workers

`bin/delete-workers` (`manage.py ci_repos remove-workers`) stops each chosen worker with `buildbot-worker stop`, deletes its folder, and makes the clones forget its checkouts (`git worktree prune`). Without `--yes` it only lists what it would do. To delete a whole repository see "Deleting, disabling and changing a repository".

| Command | Deletes |
|---|---|
| `bin/delete-workers --worker egazette-w3 --shrink --yes` | that worker, and takes it out of its repository: the worker count goes down (or its name is dropped from "Worker names"), `profiles.json` is rewritten, and the master drops the worker from Buildbot |
| `bin/delete-workers --worker egazette-w3 --keep-profile --yes` | only the folder (a fresh start); the repository still lists the worker, so `bin/create-workers` makes it again |
| `bin/delete-workers KEY --keep-profile --yes` | every worker folder of that repository, which stays listed (with `--shrink` this is refused: a repository needs at least one worker) |
| `bin/delete-workers --orphans --yes` | folders in `CI_WORKERS_DIR` that no repository lists any more (left after a lowered count or a deleted repository) |

Safety rules:

- A worker that a repository still lists is refused unless `--shrink` or `--keep-profile` says what to do with the listing.
- A worker that is running a build is skipped. Buildbot is asked; `--force` overrides this, also when Buildbot cannot be reached.
- A worker that runs under systemd is always skipped and the command to stop it is printed (`sudo systemctl disable --now buildbot-worker@<name>`), because systemd would start it again.
- Only folders directly inside `CI_WORKERS_DIR` whose `buildbot.tac` names a `workername` count as workers, so the master folder and other folders are never touched.
- `--worker` can be repeated. A worker name must belong to the `KEY` when both are given.

### Worker folders

Each worker lives in `$CI_WORKERS_DIR/<name>` (`~/.buildbotci/indianlaw-w1`, ...), made by `bin/create-workers` (which runs `buildbot-worker create-worker` with the master's address, the name and `BUILDBOT_WORKER_PASSWORD`, for every worker of every repo profile that does not exist yet). The names come from the repo profile: `<key>-w1`, `<key>-w2`, ... for the number of workers set, or the names listed in "Worker names" (to keep workers that already exist). Every worker must have the same `BUILDBOT_WORKER_PASSWORD`, and belongs to one repository. The two files in `info/` are shown on Buildbot's workers page: `info/admin` (`CI admin`, or `CI_WORKER_ADMIN` if set) and `info/host` (`<worker name> runs test modules of <repository name> (<key>)`, for example "indianlaw-w1 runs test modules of Indian Law (indianlaw)"; no folder or machine name). `bin/create-workers` writes both, and also replaces Buildbot's own placeholder texts ("Your Name Here", "Please put a description of this build host here") in workers that were made another way; texts somebody wrote are left alone. The worker sends them when it connects, so restart it after a change. The builder folders `<key>-run/`, `<key>-shard/` and `<key>-catalog/` hold the throwaway checkouts of the repository while a build runs; `runner.py sweep` removes the leftovers and looks only under `CI_WORKERS_DIR`.

## Settings

### Settings files

`~/bb-master.env` is the one settings file. Format: one `KEY='value'` per line, a comment only on a line of its own, never after a value. systemd (`EnvironmentFile=`) skips lines that start with `export` and keeps a trailing comment as part of the value; the `bin/` scripts load the file with `set -a`, and the website reads it itself. Write paths as `'$HOME/...'` (or `'~/...'`) in single quotes, or as absolute paths; the master, the runner and the website expand them for the account that runs them. Do not set `HOME=` in the file and do not nest variables. Use absolute paths in the systemd units and inside `BUILDBOT_DB_URL`, which expand nothing.

`requirements.lock` has the exact versions the website was tested with: `pip install -r requirements.lock`. `deploy/bb-master.env.example` lists every setting a server needs.

### The CI as a whole (environment of the master)

Everything that belongs to one repository is a repo profile, not a setting.

| Variable | Purpose |
|---|---|
| `BUILDBOT_DB_URL`, `BUILDBOT_WORKER_PASSWORD`, `BUILDBOT_ADMIN_PASSWORD` | required. Every worker of every repository uses the same worker password |
| `BUILDBOT_UI_PASSWORD` | password for the account the website uses (name: `BUILDBOT_UI_USER`, else `CI_BUILDBOT_USER`, else `ci-ui`); it may only force and stop builds |
| `CI_ENV` | optional: the settings file every script and `manage.py` reads (default `~/bb-master.env`). Set it by hand in each terminal (`export CI_ENV="$HOME/other.env"`; nothing is written to `~/.bashrc`), or per command (`CI_ENV=$HOME/other.env bin/master update`). It cannot be set inside the file itself. Under systemd, name the file in the unit instead (`EnvironmentFile=` for the master, `Environment=CI_ENV=...` for the website) |
| `CI_DIR` | where this repository is on the master and the workers, **required** |
| `CI_WORKERS_DIR` | folder that holds the worker folders on the worker machine (`~/.buildbotci/indianlaw-w1`, ...), **required**; the cleanup of old checkouts looks only there |
| `CI_RESULTS_DIR` | where run directories go (one folder per repository) and where `profiles.json` is, **required**. All workers and the website must see it |
| `CI_PROFILES_FILE` | optional: another place for `profiles.json` (the website, the master and the runners must all use the same) |
| `CI_MASTER_DIR` | optional: the master folder for `bin/master` (default `$CI_UI_VENV/master`) |
| `CI_UI_URL` | public URL of the website; the build log prints a link to its run page |
| `CI_UI_SITE_NAME` | the name of the site in the header, page titles, emails and Buildbot's title (default `Indian Kanoon CI`) |
| `BUILDBOT_ADMIN_USER` | login name of the administrator, default `admin`; it can also force and stop builds |
| `BUILDBOT_URL`, `BUILDBOT_WWW_PORT`, `BUILDBOT_WORKER_PORT` | `BUILDBOT_URL` is **required**; ports default to 8010 and 9989 |
| `BUILDBOT_WWW_INTERFACE`, `BUILDBOT_WORKER_INTERFACE` | make the master listen on one address only |
| `CI_ADMIN_USER`, `CI_ADMIN_PASSWORD` | optional, for a script run without a terminal (cron): the website superuser the scripts in `bin/` that change something ask for. Never put them in the shared settings file; set them for the one job |
| `CI_LOGIN_MINUTES` | how long one login for the scripts in `bin/` lasts when it is not used (default 30; `0`: every script asks) |
| `CI_AUTO_CREATE_WORKERS`, `CI_AUTO_START_WORKERS` | `0` stops the website from making the worker folders, or from starting the workers, when a repository is saved (default `1` for both; the **Create and start workers** button always works) |
| `CI_WORKER_MASTER`, `CI_WORKER_ADMIN` | used by `bin/create-workers`: how workers reach the master (default `localhost:<worker port>`), and the text of `info/admin` (default `CI admin`) |
| `CI_DB_WAIT_SECONDS` | how long `bin/wait-for-db` waits (default 90) |

**Names.** Every setting of the CI starts with `CI_` (`CI_RESULTS_DIR`, `CI_UI_SECRET`, ...), or with `BUILDBOT_` when Buildbot itself reads it. `CI_ENV` names the settings file itself.

Settings that moved into the repo profile (setting them in the file has no effect any more; `manage.py ci_repos seed-legacy` reads them once): `CI_BRANCHES`, `CI_WORKERS`, `CI_PYTHON`, `CI_REPO_DIR`, `CI_REGRESSION_PROFILE`, `CI_PROFILE_FILE`, `CI_CRON_MINUTE`, `CI_CRON_HOUR`, `CI_MAX_RUNS`, `CI_MAX_SHARDS`, `CI_MAX_PARALLEL_SHARDS`, `CI_STEP_TIMEOUT`, `CI_STEP_MAX_TIME`.

Nothing outside has to reach the master, so it works on a private network. Build logs never contain the environment of a step, because it holds the passwords.

### The website

| Variable | Purpose |
|---|---|
| `CI_UI_SECRET` | required. Signs the login cookies. Keep it the same between restarts, or everyone is signed out |
| `CI_UI_DB_URL` | database for the accounts, default `BUILDBOT_DB_URL` (the Buildbot Postgres). `postgresql://` and `sqlite:///file` are supported |
| `CI_UI_DB_SCHEMA` | Postgres schema for the account tables. Use `ciweb`: `migrate` creates it. Default is none (Postgres's normal one) |
| `CI_UI_URL` | the public address of the website. Links in emails and reset links use exactly this, and it is also the only host Django accepts besides `localhost` |
| `CI_UI_ALLOWED_HOSTS` | extra host names the website may be reached by, comma separated |
| `CI_SIGNUP_DOMAINS` | comma separated email domains, for example `indiankanoon.com`. Someone who signs up with such an address and confirms it by email gets an active account at once. Everyone else waits for an administrator. Default empty: every account waits |
| `CI_SMTP_HOST`, `CI_SMTP_PORT`, `CI_SMTP_USER`, `CI_SMTP_PASSWORD`, `CI_SMTP_FROM`, `CI_SMTP_SECURITY` | outgoing email for confirmation and reset links. Port default 587, security `starttls` (or `ssl`, `none`). Optional: without host, from address and `CI_UI_URL`, nothing is emailed and administrators issue reset links by hand |
| `CI_UI_SECURE_COOKIES` | `1` when the website is served over HTTPS, so the cookies are never sent over plain HTTP |
| `CI_UI_BEHIND_PROXY` | `1` when a reverse proxy (nginx) in front sets `X-Forwarded-Proto` |
| `CI_UI_SSL_REDIRECT`, `CI_UI_HSTS_SECONDS` | `1` and a number of seconds, to redirect plain HTTP to HTTPS and tell browsers to insist on HTTPS. Optional; the proxy can do the redirect |
| `CI_BUILDBOT_URL`, `CI_BUILDBOT_USER`, `CI_BUILDBOT_PASSWORD` | where Buildbot is (`CI_BUILDBOT_URL` is also required by the master and the runner), and the login the website uses to start runs (the master creates it from the same name, so set it the same on both). `CI_BUILDBOT_PASSWORD` is optional: without it the website uses the master's `BUILDBOT_UI_PASSWORD` |
| `CI_UI_SITE_NAME`, `CI_PROFILES_FILE`, `CI_WORKERS_DIR` | the site name; where the website writes the repo profiles (default next to the results); where the workers are, to tell administrators which are missing |
| `CI_UI_LOG_LEVEL` | how much the website logs |
| `CI_UI_DEBUG` | `0` turns off detailed error pages under `runserver` |

Serve it over HTTPS (a reverse proxy such as nginx in front of gunicorn) and set `CI_UI_SECURE_COOKIES=1` and `CI_UI_BEHIND_PROXY=1`. Without HTTPS, passwords and cookies cross the network in clear text. `python manage.py check --deploy` lists what Django still advises for your settings. Static files (the style sheet, the administration look) are served by WhiteNoise from the same process, so no extra web server is needed for them.

The website and the worker must see the same `CI_RESULTS_DIR` (same machine, or a shared mount). After adding a repository, open it and press **Refresh test list** for each branch so the picker has a test list.

### Using a settings file with another name

Both the master and the website must read the **same** file, and `CI_ENV` is how you say which. In every terminal you start them from:

```bash
export CI_ENV="$HOME/bb-master1.env"     # by hand, per terminal; not in ~/.bashrc
echo $CI_ENV
```

- The `bin/` scripts (`bin/master`, `bin/workers`, `bin/run-ui`, ...) load `$CI_ENV` themselves; `manage.py` and `runserver` read the same file by themselves. Never `source` it or export single settings by hand: a setting already exported in a terminal beats the file for the website (that is how the master and the website once ended up with different accounts and a "login failed (HTTP 401)").
- The master and the website read the file only when they start: restart both after editing it.
- To check that both agree, compare `CI_RESULTS_DIR` and `CI_BUILDBOT_USER` as the master has them (`tr '\0' '\n' < /proc/$(cat ~/.buildbotci/master/twistd.pid)/environ | grep -E '^CI_(RESULTS_DIR|BUILDBOT_USER)='`) with what a new website process gets (`python manage.py shell -c "from django.conf import settings; print(settings.RESULTS_DIR, settings.BUILDBOT_USER)"`).
- Under systemd nothing is exported: `buildbot-master.service` has `EnvironmentFile=/home/<account>/<file>`, `ci-ui.service` has `Environment=CI_ENV=/home/<account>/<file>`, both with absolute paths, and the file is owned by the service account with mode 600. The workers do not load it.

### Run modes of the website

- `python manage.py runserver` (development): loads `~/bb-master.env` by itself as plain data (never run as commands; anything already set in the terminal wins), reloads on code, template and style sheet changes, shows detailed error pages, default port 8020. It refuses detailed error pages on an address other than this machine's own. It does not notice a changed settings file.
- `manage.py migrate`, `createsuperuser`, `ci_users ...` and `ci_repos ...` find the settings the same way.
- `bin/run-ui` (real use): see "Scripts". `deploy/systemd/ci-ui.service` runs it.

## Server: systemd and the database

`deploy/` has templates (replace the account, folders and host names; nothing in it is installed by itself): `deploy/bb-master.env.example`, `deploy/systemd/` and `deploy/nginx/ci.conf` (HTTPS in front of the website).

| Unit | Notes |
|---|---|
| `buildbot-master.service` | `Requires=` and `After=postgresql@localdbs.service`; `ExecStartPre` runs `bin/wait-for-db`; `ExecReload` is `buildbot reconfig` (running builds are kept); start limit 5 in 10 minutes; memory and task limits; hardening |
| `ci-ui.service` | same database dependency and wait; runs `bin/run-ui` (migrates first, so the longer start timeout); `ExecReload` sends `HUP` to gunicorn |
| `buildbot-worker@.service` | one instance per worker; memory limit (size it as total memory minus 2 GB, divided by the number of workers); no database dependency (workers talk only to the master); `ConditionPathExists` on the worker's `buildbot.tac` so a deleted worker does not start; 90 seconds to stop, `KillMode=control-group`; does **not** load the settings file |

Why the database is `postgresql@localdbs.service` and a wait script: that unit is "started" when `pg_ctl` returns, which can be before logins work, and with `Requires=` the master and the website follow a deliberate stop or restart of Postgres. If your cluster instance has another name, change it in `buildbot-master.service` and `ci-ui.service`. The CI never installs or edits the Postgres unit itself, and `hub/tests/test_deploy_units.py` checks that the units keep these properties and that `deploy/systemd/` holds no `postgresql*` file.

### Database layout

One PostgreSQL database, `cidb`, owned by the role `ci`, with one schema for each program:

| Schema | Used by | How the program is told |
|---|---|---|
| `buildbot` | the Buildbot master (its own tables) | `BUILDBOT_DB_URL` ends with `?options=-csearch_path%3Dbuildbot` |
| `ciweb` | the website (`ci_users`, `ci_repos` and `ci_repo_branches` for the repo profiles, sessions, Django tables) | `CI_UI_DB_SCHEMA='ciweb'`; `ciweb/db.py` turns it into `-c search_path=ciweb` |

- `CI_UI_DB_URL` is the website's own address (same role and database, no `?options` part). Without it, the website falls back to `BUILDBOT_DB_URL` and drops that `options` part, because `ciweb/db.py` keeps only the SSL and timeout options of an address.
- `manage.py migrate` runs `CREATE SCHEMA IF NOT EXISTS ciweb` first, then creates the tables. Buildbot does not create its schema: create `buildbot` before `buildbot create-master`.
- Run results are not in the database. They are files under `CI_RESULTS_DIR/<repository key>/`. The repo profiles are in the database and are also written to `profiles.json` for the master and the runners. If `profiles.json` is lost, `manage.py ci_repos export` writes it again.
- To wipe one program only: `DROP SCHEMA ciweb CASCADE` removes the accounts and the repo profiles; dropping and recreating `buildbot` resets Buildbot.

## After you change something

| You changed | What to do |
|---|---|
| Website code, templates, style sheet | With `runserver`: nothing, reload the browser (Ctrl+Shift+R for the style sheet). With gunicorn: `bin/apply-update` |
| A model or a migration (`ciweb/*/migrations`) | `bin/apply-update`, or restart `bin/run-ui` (both run `migrate`; with `runserver` stop it, start it again, then run `python manage.py migrate`) |
| A repo profile (website → Manage repositories) | Nothing: the master follows within seconds. New workers: `bin/create-workers`, then `bin/workers start` |
| `runner.py` or the code a run uses | Nothing. Every Buildbot run reads the files fresh from `CI_DIR` |
| `hub/buildbot/master.cfg` | `bin/master update` |
| `hub/repos.py`, `hub/adapters/`, `hub/buildbot/repo_config.py` (read from `CI_DIR` on every reconfiguration) | `bin/master reconfig`, no copy |
| `~/bb-master.env` | Restart the program that reads it (the master, the worker, `runserver` or `bin/run-ui`) |
| `requirements.txt` | `bin/apply-update` |
| `deploy/systemd/*.service` | Copy to `/etc/systemd/system/`, then `sudo systemctl daemon-reload` and restart the unit |

## The website: layout and warnings

**Look.** The reader picks one of four colour themes with the swatches at the bottom of the sidebar (kept in the browser, applied before the page is drawn): **Paper** (default: warm ivory page, deep teal), **Ocean** (cool page, dark blue sidebar), **Slate** (neutral grey, indigo, dark sidebar) and **Midnight** (dark; also the default on a system that asks for a dark screen). A theme is only a block of colour variables at the top of `app.css` (`:root[data-theme="..."]`, including the sidebar's `--nav-*`). To add one, copy a block, give it a name, add a swatch in `base.html` and `app.js`; the tests check that it defines every variable and that every text colour has at least 4.5 to 1 contrast. The text size starts at 100% of the reader's setting and grows gently with the screen (`html { font-size: clamp(...) }`); body text is 1rem, small text 0.9rem, and nothing is smaller than 0.8rem. Buttons and fields are at least 2.75rem high (`--touch`) so a finger can hit them. Breakpoints (in `rem`): above 90rem the page is centred and stops growing; up to 70rem a narrower sidebar; up to 56rem the sidebar becomes a drawer behind a menu button; up to 40rem one column, stacked buttons, full-width dialogs, tables that scroll sideways. Times and sizes are written out ("5 minutes 19 seconds", "2.0 kilobytes").

**Layout.** The site is called by `CI_UI_SITE_NAME` (default **Indian Kanoon CI**). Every page has a sidebar (a drawer on a phone): Overview, each repository with a status dot (latest result; blue and pulsing while a run is in progress) and, for the open repository, its branches and **Run tests**; then, for administrators, **Manage repositories** and **People**; and at the bottom the account. The page itself starts with a title and its main buttons. Long forms (add and edit repository) end with their buttons as ordinary content, never fixed to the screen. The variable for the name in templates is `ci_name` (not `site_name`: Django's login view sets that one to the host name).

**The add and edit repository form** (`ciweb/repos/templates/repos/edit.html`, `static/repos/repo_form.js`, `ciweb/repos/probe.py`) is four steps, with links at the top that turn green as each is filled in: **1 Code** (the clone folder comes first, everything else follows from it), **2 Tests** (the test suite type as cards, each with a one-line `Adapter.best_for`), **3 Branches**, **4 Workers**. While the administrator types the clone folder or the Python, the page asks `POST /settings/repos/probe` (administrators only, read-only: it asks git a few questions and looks for files, and never runs a program of the repository or shows a password from the address of `origin`). The answer fills in what was not typed by hand: the key and name (from the folder name), the checkout name, the Python (`~/.<key>/bin/python` or the clone's `.venv` when one exists; the default `python3` may be replaced, a value somebody typed never is), the test suite type (`regressions/regression.py` means Output comparison, pytest files mean pytest, otherwise Command checks; the card says "Looks right" and why), and the branches known to the clone (as of its last fetch), which appear as buttons that add or remove a branch row, with the clone's default branch added to a new repository. Less common settings (description, checkout name, environment variables, worker names and limits) are under **Advanced** disclosures that open by themselves when one has an error. A sentence above the buttons says what Save will create ("Indian Law: Output comparison, 3 branches (1 tested every night), 3 workers."). The buttons stay at the end of the form, never fixed to the screen. The settings of a test suite type still come from `Adapter.fields`; a new type only adds `label`, `summary` and `best_for`.

**The overview** (`/`) has no counters ("1 repository, 0 runs in progress" says nothing a reader can act on). It opens with a big coloured headline that says how things are, in the colour of the worst thing: red "2 branches are failing" (with a link to each failing run and how many tests failed), amber when runs are stuck, blue while tests run, green "Everything is passing", grey before anything was tested (`views.overview_hero`; the class is `.health-hero`, not `.hero`, which the run page uses). Then one card for each repository, with a stripe and a label in its own health colour, a strip of the last 12 results of each branch (one bar for each run, newest on the right and taller; hover for the result and its age), the latest result with its failing count, revision and age, the nightly time, what is in progress (the same numbers as the sidebar) and a **Run tests** button. The card's title is its link (a stretched link, so the button inside it is a real button).

**The repository page** (`/r/<key>/`) answers "can a run start?" before anything is pressed: a strip of two facts (**Workers** connected, **Nightly runs**) and, when nothing can run, an alert that says why and what to do: the repository is disabled; Buildbot cannot be reached (a three second wait at most, so the page never hangs); no worker is connected (it counts the runs waiting, and offers administrators **Create and start workers**); or Buildbot could not load the repository's settings (the only time the page speaks about Buildbot's settings: when they are fine nothing is said).

**What "in progress" means** (`ciweb/runs/activity.py`). A run's record (`run.json`) says `running` until the run writes its end, and nothing writes it when Buildbot gives a build back (state RETRY, for example after its worker went away) or loses it. So the sidebar, the overview and the repository page never trust the record alone: they ask Buildbot (`buildbot_client.live_runs`: executing builds and requests no worker has taken; remembered 5 seconds, a failure too) and show each run as **running** (a build is executing), **waiting for a worker** (a request waits, which is what a RETRY leaves) or **stalled** (the record says in progress, Buildbot has nothing; not counted as in progress; pressing Stop closes it as aborted). A run younger than 90 seconds keeps its record, and when Buildbot cannot be asked every record is shown as written. All three pages use `activity.settle` and `activity.summary`, so their numbers cannot differ. **Add branch** is a small button next to the Branches heading. The test list (`/r/<key>/new` ) keeps the modules in the catalog's order, has **Expand all / Collapse all**, and shows no failing counts or "select failing" shortcut (the run page has **Re-run failing**).

**Every risky action asks first, in a dialog** (one shared `<dialog id="confirm-dialog">` in `base.html`, driven by `app.js`). Put `data-confirm="what happens"`, `data-confirm-title`, `data-confirm-items="one|two"`, `data-confirm-ok` and `data-confirm-tone="danger|warn"` on a form or its submit button, or call `window.ciConfirm({...}, callback)` from script. Used for: stopping a run, disabling a repository, removing a branch, rejecting or disabling a person. Deleting a repository has its own dialog that lists what the ticked options will delete (`repos/delete.html`). Without JavaScript the forms are sent without the dialog; the server still requires the typed key for a delete, and the confirmation page for stopping someone else's run. Do not use `window.confirm`.

## Accounts

Developers need an account to start a run or refresh a test list. Reading runs, logs and differences needs no login. This is Django's own account system, with a few additions.

- **Where they live:** Django's tables in the database named by `CI_UI_DB_URL`, in the schema `CI_UI_DB_SCHEMA`. The users are in the table `ci_users`. They are separate from Buildbot's own tables, and Buildbot never touches them.
- **Passwords:** stored only as **Argon2id** hashes with a random salt for each password, never encrypted and never readable. A password needs at least 10 characters, must not be a common or all-number one, and must not contain the user name or email.
- **Sign up:** `/signup`. The account starts as `pending` and cannot sign in. It becomes active when an administrator approves it, or when the owner confirms the emailed link and the domain is in `CI_SIGNUP_DOMAINS`.
- **Sign in:** `/login`, by user name or email, in any letter case. Five wrong passwords lock the account for 15 minutes. The messages never say whether the name or the password was wrong. The words "waiting for approval" or "disabled" only appear after the right password.
- **Forgot password:** `/forgot` emails a one-time link that is valid for 1 hour and stops working as soon as the password is changed. Asking for it never reveals whether an address has an account, and only active accounts get one. Using the link signs the account out everywhere. Without email settings, an administrator issues the link (below).
- **Change password:** `/account/password`, which needs the current password and signs out the other devices.
- **Sessions:** server-side rows in `django_session`, valid for 12 hours, in a cookie that is `HttpOnly` and `SameSite=Lax`. Disabling an account or changing its password ends its sessions at once.
- **Forms:** every form carries a secret token (Django's CSRF protection), so another website cannot make your browser start a run.
- **Approval queue:** administrators (accounts marked staff) see a **People** link with a red count in the header and a banner on every page while anyone is waiting. The **People** page (`/people`) lists the waiting sign-ups with **Approve** and **Reject** buttons, and everyone else with **Disable**, **Enable** and **Unlock**. Rejecting removes the sign-up, so the person can try again. Nobody can disable themselves, and ordinary users cannot open the page.
- **Email about approvals:** with email set up, every active administrator is emailed when someone signs up (name, email address and a link to the People page), and the person is emailed when they are approved or rejected. Without email settings, the banner and the People page still work; an administrator just has to look.
- **Header:** signed-in people see their name in an account chip (it opens their account page) and a **Sign out** button on every page.

The run list records the signed-in name, and scheduled runs are recorded as `Auto`, a name nobody can register.

**Administrator tools.** In the same environment as the website (`manage.py` reads `~/bb-master.env` by itself):

```bash
cd ~/buildbotci
python manage.py ci_users list                          # everyone, with state
python manage.py ci_users add alice alice@example.com   # active at once; asks for the password
python manage.py ci_users approve bob                   # let a pending account in (also: enable)
python manage.py ci_users disable bob                   # stop an account signing in
python manage.py ci_users reset bob                     # print a one-time reset link to send them
python manage.py ci_users passwd bob                    # set a new password yourself
python manage.py ci_users unlock bob                    # clear a lock from wrong passwords
python manage.py ci_users mail-test me@example.com      # check the SMTP settings
```

The People page covers the daily work in a browser. For everything else, `/admin/` is available to accounts marked staff (the one from `createsuperuser` is); the People page does not point to it: a list of users with filters, and the actions **Approve**, **Disable** and **Unlock** for the ones you tick. Whoever can run `manage.py` can reach the database, so it is the administrator. Not included: two-factor sign-in, and limits per network address. Buildbot's own web page keeps its separate `admin` and website logins from the settings above.

## Run directory layout

Each repo profile has its own folder inside the results folder, so results are kept per repository:

```
$CI_RESULTS_DIR/
  profiles.json                 the repo profiles, written by the website (mode 600); read by the master and the runners
  profiles.applied.json         what the master reports it has loaded
  cleanup.json                  builders whose Buildbot history the website asked to erase
  <repo key>/
    catalogs/<branch>.json      tests and modules of each branch, refreshed by every run (a branch release/1.2 is release__1.2.json)
    state/last_tested.json      last scheduled commit per branch
    runs/<run id>/
      run.json                  repository, suite type, state, requested by, selection, per-test status, counts, shards (written by plan and finalize)
      shards/<n>.json           one per module build: worker, state, build link, its tests' status (written live by that worker)
      stop.json                 the request to stop the run, if any
      runner.log                console output of the run, shard lines prefixed with [shard n]
      tests/<test id>/
        result.json             status, reason, timings, hunks, exception, how to reproduce it
        log.txt                 stdout, stderr and logging of that test
        actual, expected, diff.txt   only kept when the test did not pass (diff.txt and expected only for suites that compare output)
```

Deleting a repo profile keeps its folder unless you ask to delete the results. Results from before there were repo profiles are moved into `indianlaw/` by `manage.py ci_repos seed-legacy`.

Every failed test keeps its **complete** files: `diff.txt` (the whole unified diff made by git's histogram algorithm, however large), `expected` and `actual`. The test page shows them in a **Full files** box at the top, with sizes, download buttons and the exact path on disk, and the Buildbot log of the module prints the same paths (and, with `CI_UI_URL` set, links) right under each failed test.

The runner cuts the diff at record boundaries, so a region never mixes two documents, cases or blocks (for example the end of one document and the header of the next). In the Output comparison type, how an expected file is divided into records is described by `record_formats` in `hub/impact_map.json`. Each rule names the output files it applies to (`outputs`, glob patterns) and then either `start` (alternative lists of regular expressions for consecutive lines that begin a record, for example `=====`, `docid:`, `RELURL:`) or `key` (a regular expression whose group is the record's key; a new record begins when the key changes, as in the `resolve_*` CSV files, where many rows share a docid). `label` is an optional regular expression with a group, looked for in the first lines of a record (by default the first line of the record is the label). Rules exist for every output of every module that has a record structure: layout documents, `header` cases, `structural` blocks, `FILE:` texts, cited-document files, resolution CSVs, causelists, lawsik, query `TEST` blocks and the files with one record per line. Outputs without a rule (for example `xml_deserialization`) are shown as git made them, and so are the golden-file differences of the `command` type. To support a new output, add a rule; a rule that finds no record is ignored. Only context lines of the neighbouring record are dropped; every changed line is kept, in order. The label of each region is written after its `@@ … @@` line in `diff.txt`, like git's function context. The Diff tab shows the differences in pages of 15 documents: all the differing regions of one document are shown together under one number, with the count of regions. The downloads are the whole file. Runs saved before this existed only know the document of their first 1000 regions; the rest show one number per region.

Passing tests keep only `log.txt` and `result.json`, because the expected files together are about 0.5 GB. Nothing prunes old runs by itself: run `bin/prune-runs --days N --yes` (without `--yes` it only lists) from cron.

## Using it from a shell

Every runner command needs the repo profile (`--repo KEY`, or `CI_REPO`) and finds `profiles.json` through `CI_RESULTS_DIR` (or `--profiles-file`). With the settings file loaded (`set -a; . ~/bb-master.env; set +a`):

```bash
python runner.py repos                                       # the repo profiles
python runner.py --repo indianlaw sync --branch 2026         # git fetch origin, print the latest commit
python runner.py --repo indianlaw catalog --branch 2026      # read the test list of origin/2026
python runner.py --repo indianlaw select                     # what would run for changes since last tested
python runner.py --repo indianlaw run --tests "header.bombay_hdr layout"
python runner.py --repo egazette run --all                   # one process, no Buildbot, no shards (uses the clone as it is)
python runner.py prune --days 30                             # all repositories; add --repo KEY for one
```

Run it with the repository's own Python (the one in its profile). The original command line of `indianlaw` is unchanged and does not use the CI: `python regression.py -P local -m layout -t analyze_paras`, and `-p` to write or refresh an expected file.

Repo profiles from a shell (in the website's environment; `manage.py` reads `~/bb-master.env` by itself):

```bash
python manage.py ci_repos list                  # the profiles, their branches and workers
python manage.py ci_repos export                # write profiles.json again from the database
python manage.py ci_repos workers [KEY]         # which workers exist on this machine (--create makes them)
python manage.py ci_repos start-workers|stop-workers|restart-workers|status-workers [KEY] [--worker NAME]   # what bin/workers runs
python manage.py ci_repos remove-workers ...    # what bin/delete-workers runs
python manage.py ci_repos remove KEY [--yes]    # delete a whole repository
python manage.py ci_repos seed-legacy --dry-run # make the indianlaw profile from the old settings and move the old results
```

## Tests

Run this repository's own tests in the Buildbot environment (it has Django, argon2 and Buildbot):

```bash
cd ~/buildbotci
python manage.py test ciweb                                   # the website, the repo profiles, the accounts, worker control
PYTHONPATH=. python -m unittest discover -s hub/tests -t .    # runner, adapters, git handling, store, differences, master configuration, systemd units
```

The contract tests need the project's own Python, so run them there: `python -m unittest hub.tests.test_contract`. They are skipped when `indianlaw` is not next to this folder (set `CI_REPO_DIR`) or its branch has no `regression.MODULES`. The pytest adapter tests need pytest in the Python that runs them (they are skipped otherwise). The website tests use a temporary SQLite database and temporary folders; they never touch Postgres or your real worker folders.
