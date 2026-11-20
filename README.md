# Regression CI

Buildbot runs the `indianlaw` regression tests on a schedule and on demand. A small web UI lets developers pick tests, start runs, watch them, read logs, compare expected and actual output, and see why a test failed.

This is a separate repository. The `indianlaw` repository contains no CI code and never refers to this one. The CI reads the tests from the `indianlaw` repository that sits **next to this folder**, through a small contract (see below). There is no GitLab webhook and nothing is cloned: the CI runs `git fetch origin` in that repository.

```
~/
  buildbotci/     this repository (the CI)
  indianlaw/      your clone of the application repository, with its remote called origin
```

```
every night ──────────────┐                        ┌─► worker 1: module A ─┐
                          ├─► Buildbot master ─────┼─► worker 2: module B ─┼─► $REG_RESULTS_DIR
developer, Web UI ────────┘   (one run, N builds)  └─► worker 3: module C ─┘          ▲
Web UI (ciweb, Django) ───────────── reads run directories ──────────────────────────┘
```

Each module of a run goes to its own worker. A run with 5 modules and 3 workers runs 3 modules at a time and queues the other 2 for whichever worker frees up first. A worker never runs two modules at once.

| Part | Where |
|---|---|
| Structured runner (`sync`, `checkout`, `release`, `plan`, `shard`, `finalize`, `catalog`, `run`, `select`) | `runner.py` |
| Run directory format | `hub/store.py` |
| Diff parsing and failure localisation | `hub/diffparse.py` |
| Changed files to tests (command line only) | `hub/selection.py`, `hub/impact_map.json` |
| Web UI (Django project) | `ciweb/`: `ciweb/runs/` (run pages), `ciweb/accounts/` (users, sign-in, admin), `manage.py` |
| Buildbot master config | `hub/buildbot/master.cfg` |
| Tests for all of the above | `hub/tests/` |

## How the CI reads `indianlaw`

The CI only watches the branches in `REG_BRANCHES` (default `main` and `2026`), and only through `origin`:

- Every run starts with `git fetch origin` in the `indianlaw` folder next to this one, and takes the latest commit of `origin/<branch>`.
- It **never** switches branches, resets files, or writes into your working copy. Each run, and each worker, gets its own throwaway checkout made with `git worktree` from that repository. They share its objects, so nothing is cloned again. A run's checkout is removed when the run ends.
- Git commands on that repository are serialised with a lock file (`.git/ci.lock`), so concurrent runs cannot collide.
- On a machine with several workers, each machine needs its own `buildbotci` and `indianlaw` folders, at the same path as on the master (see `REG_CI_DIR`).

It is safe to point the CI at the `indianlaw` folder you develop in. Do not delete worker checkouts by hand: use `git worktree prune` in that repository if you ever do.

## The contract with `indianlaw`

`runner.py` imports `regressions/regression.py` from a checkout of `indianlaw`. It needs these names, and nothing else, from it:

| Name | Meaning |
|---|---|
| `MODULES` | dict of module name to `(class, factory)`; `factory(storage, datasrcs)` builds the object |
| `get_class_tests(cls)` | names of the test methods of a class |
| `output_filename(test)` | name of the expected file of a test |
| `compare_files(expected, actual, labels)` | `(status, unified diff)` |
| `load_regress_objs(storage, datasrcs, modules)` | objects for the given module names |
| `PASSED`, `FAILED`, `ERROR`, `NO_EXPECTED` | status strings |

It also imports `indianlaw.setup.data_srcs.DataSrcFactory`, `indianlaw.utils.commonfuncs.setup_logging` and `indianlaw.storage_manager.StorageManager`, and reads `regressions/expected/`.

A branch without `MODULES` in its `regression.py` cannot be tested. `hub/tests/test_contract.py` checks all of this against a real checkout. Run it after every change to `regression.py`. It is skipped when there is no checkout.

The checkout folder must be named `indianlaw`, because the code imports `indianlaw.*`.

## What happens on a schedule

1. At each scheduled time (every night at 21:00 IST in your setup), the master starts one `regression` build per watched branch.
2. The first step runs `git fetch origin` and compares `origin/<branch>` with the last commit that was scheduled for that branch (`state/last_tested.json`). If it is the same commit, the build ends there with "No new commits. Nothing to run." Otherwise the commit is recorded and the run goes on.
3. A scheduled run tests **all modules** of that commit and is recorded as requested by `Auto`. The first scheduled check after a fresh install always runs, since nothing was recorded yet.
4. The build makes a private checkout of that commit and runs `runner.py plan`, which groups the tests by module. Each module becomes one shard.
5. The build triggers one `regression-shard` build per module. Buildbot assigns each to a free worker, which updates its own checkout to the same commit and runs `runner.py shard` for that module only. Each test runs in isolation, and failures never stop the shard. Each shard writes its own progress file, so the UI shows live progress per worker.
6. When every shard has ended, the `finalize` step (it always runs) merges the shards into the final run result. A shard that never reported, because its worker was lost or cancelled, marks its tests `aborted` and the run `aborted`.
7. `finalize` exits 1 if any test is not `passed`, so the Buildbot build goes red. The private checkout of the run is removed.

## What happens on a manual run

A developer signs in to the web UI, picks a branch (`main` or `2026`) and the modules or tests to run, and clicks **Queue run**. The same steps follow, except: `git fetch origin` is always followed by a run (it never skips), only the selected tests run, and the run is recorded under the developer's name. The reason shown in Buildbot starts with that name.

The test list in the picker belongs to the branch you chose, and is read from `origin/<branch>` by Buildbot (`catalog`). Every run refreshes the list of its branch. The **Refresh from GitLab** button refreshes it without running anything.

### Runs overlap

Runs do not wait for each other. Each run has its own checkout and run directory, and a worker handles one module at a time. `REG_MAX_RUNS` (default 8) caps how many runs are in progress at once, and an extra run waits.

What is **not** isolated: the databases. Two runs at the same time can run the same module against the same databases. If a module writes shared data (for example `header` and `layout` can write TIDDB), two overlapping runs can disturb each other's results. Check that before relying on overlapping runs.

## Test statuses

| Status | Meaning |
|---|---|
| `passed` | Output identical to `regressions/expected/<name>`, or a non-output test returned truthy |
| `failed` | Output differs, or a non-output test returned falsy |
| `no_expected` | The test ran but `expected/<name>` does not exist. This is a failure |
| `error` | The test, or its module's setup, raised. Traceback, partial output and log are kept |
| `aborted` | Buildbot cancelled or timed out the run (the runner gets SIGTERM first) |

## Choosing tests

- **Scheduled runs**: every module.
- **Manual runs**: in the web UI, tick modules or single tests, or pick everything. Module names (`header`), test ids (`header.bombay_hdr`), globs (`*.aft_*`) and `all` are all accepted, which is also what the Buildbot "Run regressions" form takes in its Tests box. An empty selection means all.

The impact map (`hub/selection.py`, `hub/impact_map.json`) is not used by Buildbot or the web UI. It only serves the command line: `python runner.py select --base <rev>` previews which tests changed files would map to, and `runner.py run` with no arguments uses it.

## Setup

Everything that belongs to Buildbot (master, worker, web UI) can live in one virtual environment. The tests themselves run with the project's own Python (`REG_PYTHON`), which has the application's libraries.

### Folders

On every machine that runs a worker, and on the master machine: this repository at `~/buildbotci` and your clone of `indianlaw` at `~/indianlaw`, with `git fetch origin` working without a password (a deploy key or your SSH key). If they live elsewhere, set `REG_CI_DIR` (and `REG_REPO_DIR`) on the master.

### Buildbot environment

```bash
python3 -m venv ~/.buildbotci && . ~/.buildbotci/bin/activate
pip install buildbot buildbot-www buildbot-worker -r ~/buildbotci/requirements.txt
```

### Master

```bash
createdb buildbot
export BUILDBOT_DB_URL='postgresql+psycopg2://user:password@host/buildbot'
buildbot create-master --db "$BUILDBOT_DB_URL" ~/.buildbotci/master
cp ~/buildbotci/hub/buildbot/master.cfg ~/.buildbotci/master/master.cfg
buildbot checkconfig ~/.buildbotci/master && buildbot start ~/.buildbotci/master
```

### Worker (host with the data directory and database access)

```bash
buildbot-worker create-worker ~/.buildbotci/w1 <master-host>:9989 w1 "$BUILDBOT_WORKER_PASSWORD"
buildbot-worker start ~/.buildbotci/w1
```

Repeat on every worker host with its own name, and list all the names in `REG_WORKERS` on the master. One worker works too, it then runs the modules one after another.

The worker needs `git`, the project's Python environment (`REG_PYTHON`, for example `~/.indianlaw/bin/python`), access to `DATADIR` and Postgres, and write access to `$REG_RESULTS_DIR`, which every worker and the UI must see at the same path (shared mount). Profile settings come from the checkout's `.env.<profile>` (`REGRESSION_PROFILE`, default `local`). The `DATADIR` in that file must exist on the worker.

### Settings (environment of the master)

| Variable | Purpose |
|---|---|
| `BUILDBOT_DB_URL`, `BUILDBOT_WORKER_PASSWORD`, `BUILDBOT_ADMIN_PASSWORD` | required |
| `BUILDBOT_UI_PASSWORD` | password for the `regression-ui` account the web UI uses; it may only force and stop builds |
| `REG_CI_DIR` | where this repository is on the workers, default `~/buildbotci` |
| `REG_REPO_DIR` | where `indianlaw` is on the workers, default the folder `indianlaw` next to `REG_CI_DIR` |
| `REG_BRANCHES` | comma separated branches of `indianlaw` to watch and offer in the web UI, default `main,2026`. Set it for both the master and the web UI |
| `REG_CRON_MINUTE`, `REG_CRON_HOUR` | when the scheduled check runs, in cron syntax, defaults `0` and `*` (every hour on the hour). Nightly at 21:00: minute `0`, hour `21`. Every 30 minutes: minute `0,30`. The time zone is the master's `TZ`. A check with no new commit does nothing |
| `REG_WORKERS` | comma separated worker names, default `staging`; every worker needs the same `BUILDBOT_WORKER_PASSWORD` |
| `REG_MAX_RUNS` | most runs in progress at once, default 8 |
| `REG_MAX_SHARDS` | most modules running as separate builds in one run, default 32; with more modules they are grouped |
| `REG_RESULTS_DIR` | where run directories go, default `~/regression-results` |
| `REG_UI_URL` | public URL of the web UI; the build log prints a link to its run page |
| `REG_PYTHON` | interpreter on the worker that runs the tests, default `python3` |
| `REGRESSION_PROFILE` | profile, default `local` |
| `REG_PROFILE_FILE` | optional path, **on each worker**, of a private file with only the settings that differ on that machine, for example `DATADIR=/home/you/indianlaw_data`. Each module build merges it into `.env.<REGRESSION_PROFILE>` in its checkout: the settings you list replace the committed ones, and every other setting is kept (the build log names the settings it changed, never their values). Needed because the code loads `.env.<profile>` from the checkout with override, so a `DATADIR` environment variable has no effect |
| `BUILDBOT_ADMIN_USER`, `BUILDBOT_UI_USER` | login names, default `admin` and `regression-ui`; the admin can also force and stop builds |
| `BUILDBOT_URL`, `BUILDBOT_WWW_PORT`, `BUILDBOT_WORKER_PORT` | default `http://localhost:8010/`, 8010, 9989 |
| `REG_STEP_TIMEOUT`, `REG_STEP_MAX_TIME` | seconds without output (7200) and total (21600) |

Nothing outside has to reach the master, so it works on a private network.

### Web UI

A Django project in `ciweb/`. It needs the packages in `requirements.txt` (Django, argon2-cffi, WhiteNoise, requests, psycopg2, gunicorn), read access to `$REG_RESULTS_DIR`, and a database for the accounts. It does not import the application code.

**First time, and after every update of this repository:**

```bash
cd ~/buildbotci
. ~/bb-master.env                      # the same settings file as the master (see the table below)
export REG_UI_DB_SCHEMA=ci             # keeps the account tables apart from Buildbot's own
python manage.py migrate               # creates or updates the account tables
python manage.py createsuperuser       # your own administrator account (asks for a password)
```

**While you work on the code, just use Django's own server:**

```bash
cd ~/buildbotci
python manage.py runserver        # http://127.0.0.1:8020
```

Nothing else to load first: `manage.py` reads `~/bb-master.env` by itself (as plain data; it never runs it as commands, and anything already set in your terminal wins). The server reloads by itself when a Python file, a template or the style sheet changes (`django.utils.autoreload`). In this mode Django shows its detailed error pages in the browser and prints tracebacks in the terminal, and the default port is 8020 to match `REG_UI_URL`. To turn the detailed pages off, set `REG_UI_DEBUG=0`. `runserver` refuses to start with them on an address other than this machine's own, so they can never be exposed by accident. It is for your own machine only: it is not built for real traffic, and it does not notice a changed settings file (stop it with Ctrl+C and start it again). Reload the browser page after an edit (Ctrl+Shift+R for the style sheet).

`python manage.py migrate`, `createsuperuser` and `ci_users ...` find the settings the same way. `REG_ENV_FILE` points them at another file.

**For real use, start the site with gunicorn:**

```bash
cd ~/buildbotci
bin/run-ui
```

It loads `~/bb-master.env`, applies any database updates, then starts gunicorn on `http://127.0.0.1:8020`, with debug pages off. To change what it uses, set `REG_UI_VENV` (default `~/.buildbotci`), `REG_ENV_FILE`, `REG_UI_BIND` (default `127.0.0.1:8020`) or `REG_UI_WORKERS` (default 2). It records its process ID in `~/.buildbotci/ui.pid` (`REG_UI_PID_FILE`).

**Applying an update on a machine where the site keeps running: `bin/apply-update`.** After the files are updated (`git pull`, or copied), it installs `requirements.txt`, applies any database updates, and tells the running site to reload with a graceful signal, so no request is dropped.

**After you change something:**

| You changed | What to do |
|---|---|
| Web UI code, templates, style sheet | With `python manage.py runserver`: nothing. With gunicorn: `bin/apply-update`. Reload the browser page (Ctrl+Shift+R for the style sheet) |
| A model or a migration (`ciweb/accounts/migrations`) | `bin/apply-update`, or stop and start `bin/run-ui` (both run `migrate`; with `runserver` stop it and start it again, then run `python manage.py migrate`) |
| `runner.py` or the code a run uses | Nothing. Every Buildbot run reads the files fresh from `REG_CI_DIR` |
| `hub/buildbot/master.cfg` | `cp ~/buildbotci/hub/buildbot/master.cfg ~/.buildbotci/master/`, then `buildbot checkconfig ~/.buildbotci/master && buildbot reconfig ~/.buildbotci/master` (running builds are not lost) |
| `~/bb-master.env` | Stop and start the program that reads it (the master, the worker, `runserver` or `bin/run-ui`) |
| `requirements.txt` | `bin/apply-update` |

The long form of the start command is `gunicorn -w 2 -b 127.0.0.1:8020 ciweb.wsgi:application`, run after loading the settings, if you ever need it.

| Variable | Purpose |
|---|---|
| `REG_UI_SECRET` | required. Signs the login cookies. Keep it the same between restarts, or everyone is signed out |
| `REG_UI_DB_URL` | database for the accounts, default `BUILDBOT_DB_URL` (the Buildbot Postgres). `postgresql://` and `sqlite:///file` are supported |
| `REG_UI_DB_SCHEMA` | Postgres schema for the account tables. Use `ci`: `migrate` creates it. Default is none (Postgres's normal one) |
| `REG_UI_URL` | the public address of the UI. Links in emails and reset links use exactly this, and it is also the only host Django accepts besides `localhost` |
| `REG_UI_ALLOWED_HOSTS` | extra host names the UI may be reached by, comma separated |
| `REG_SIGNUP_DOMAINS` | comma separated email domains, for example `indiankanoon.com`. Someone who signs up with such an address and confirms it by email gets an active account at once. Everyone else waits for an administrator. Default empty: every account waits |
| `REG_SMTP_HOST`, `REG_SMTP_PORT`, `REG_SMTP_USER`, `REG_SMTP_PASSWORD`, `REG_SMTP_FROM`, `REG_SMTP_SECURITY` | outgoing email for confirmation and reset links. Port default 587, security `starttls` (or `ssl`, `none`). Optional: without host, from address and `REG_UI_URL`, nothing is emailed and administrators issue reset links by hand |
| `REG_UI_SECURE_COOKIES` | `1` when the UI is served over HTTPS, so the cookies are never sent over plain HTTP |
| `REG_UI_BEHIND_PROXY` | `1` when a reverse proxy (nginx) in front sets `X-Forwarded-Proto` |
| `REG_UI_SSL_REDIRECT`, `REG_UI_HSTS_SECONDS` | `1` and a number of seconds, to redirect plain HTTP to HTTPS and tell browsers to insist on HTTPS. Optional; the proxy can do the redirect |
| `REG_BRANCHES` | must match the master's |
| `REG_BUILDBOT_URL`, `REG_BUILDBOT_USER`, `REG_BUILDBOT_PASSWORD` | where Buildbot is, and the `regression-ui` login (`BUILDBOT_UI_PASSWORD` of the master) the UI uses to start runs |
| `REG_BUILDBOT_FORCE_SCHEDULER`, `REG_BUILDBOT_CATALOG_SCHEDULER` | default `force-regression` and `force-catalog` |

Serve it over HTTPS (a reverse proxy such as nginx in front of gunicorn) and set `REG_UI_SECURE_COOKIES=1` and `REG_UI_BEHIND_PROXY=1`. Without HTTPS, passwords and cookies cross the network in clear text. `python manage.py check --deploy` lists what Django still advises for your settings. Static files (the style sheet, the admin look) are served by WhiteNoise from the same process, so no extra web server is needed for them.

The UI and the worker must see the same `REG_RESULTS_DIR` (same host, or a shared mount). The first time, open the start page and press **Refresh from GitLab** for each branch so the picker has a test list.

### Accounts

Developers need an account to start a run or refresh a test list. Reading runs, logs and diffs needs no login. This is Django's own account system, with a few additions.

- **Where they live:** Django's tables in the database named by `REG_UI_DB_URL`, in the schema `REG_UI_DB_SCHEMA`. The users are in the table `ci_users`. They are separate from Buildbot's own tables, and Buildbot never touches them.
- **Passwords:** stored only as **Argon2id** hashes with a random salt for each password, never encrypted and never readable. A password needs at least 10 characters, must not be a common or all-number one, and must not contain the user name or email.
- **Sign up:** `/signup`. The account starts as `pending` and cannot sign in. It becomes active when an administrator approves it, or when the owner confirms the emailed link and the domain is in `REG_SIGNUP_DOMAINS`.
- **Sign in:** `/login`, by user name or email, in any letter case. Five wrong passwords lock the account for 15 minutes. The messages never say whether the name or the password was wrong. The words "waiting for approval" or "disabled" only appear after the right password.
- **Forgot password:** `/forgot` emails a one-time link that is valid for 1 hour and stops working as soon as the password is changed. Asking for it never reveals whether an address has an account, and only active accounts get one. Using the link signs the account out everywhere. Without email settings, an administrator issues the link (below).
- **Change password:** `/account/password`, which needs the current password and signs out the other devices.
- **Sessions:** server-side rows in `django_session`, valid for 12 hours, in a cookie that is `HttpOnly` and `SameSite=Lax`. Disabling an account or changing its password ends its sessions at once.
- **Forms:** every form carries a secret token (Django's CSRF protection), so another website cannot make your browser start a run.
- **Approval queue:** administrators (accounts marked staff) see a **People** link with a red count in the header and a banner on every page while anyone is waiting. The **People** page (`/people`) lists the waiting sign-ups with **Approve** and **Reject** buttons, and everyone else with **Disable**, **Enable** and **Unlock**. Rejecting removes the sign-up, so the person can try again. Nobody can disable themselves, and ordinary users cannot open the page.
- **Email about approvals:** with email set up, every active administrator is emailed when someone signs up (name, email address and a link to the People page), and the person is emailed when they are approved or rejected. Without email settings, the banner and the People page still work; an administrator just has to look.
- **Header:** signed-in people see their name in an account chip (it opens their account page) and a **Sign out** button on every page.

The run list records the signed-in name, and scheduled runs are recorded as `Auto`, a name nobody can register.

**Administrator tools.** In the same environment as the UI (the settings loaded as above):

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

The People page covers the daily work in a browser. For everything else, `/admin/` is available to accounts marked staff (the one from `createsuperuser` is): a list of users with filters, and actions **Approve**, **Disable** and **Unlock** for the ones you tick. Whoever can run `manage.py` can reach the database, so it is the administrator.

Not included: two-factor sign-in, and limits per network address. Buildbot's own web page keeps its separate `admin` and `regression-ui` logins from the settings above.

## Run directory layout

```
$REG_RESULTS_DIR/
  catalogs/<branch>.json        tests and modules of each branch, refreshed by every run
  state/last_tested.json        last scheduled commit per branch
  runs/<run id>/
    run.json                    state, requested by, selection, per-test status, counts, shards (written by plan and finalize)
    shards/<n>.json             one per module build: worker, state, build link, its tests' status (written live by that worker)
    runner.log                  console output of the run, shard lines prefixed with [shard n]
    tests/<module.test>/
      result.json               status, reason, timings, hunks, exception
      log.txt                   stdout, stderr and logging of that test
      actual, expected, diff.txt   only kept when the test did not pass
```

Every failed test keeps its **complete** files: `diff.txt` (the whole unified diff, however large), `expected` and `actual`. The test page shows them in a **Full files** box at the top, with sizes, download buttons and the exact path on disk, and the Buildbot log of the module prints the same paths (and, with `REG_UI_URL` set, links) right under each failed test. The Diff tab shows the diff in pages of 15 regions; the downloads are the whole file.

Passing tests keep only `log.txt` and `result.json`, because the expected files together are about 0.5 GB. **Nothing prunes old runs.** Add a cron job that removes run directories older than you want to keep.

## Using it from a shell

With this repository next to a clone named `indianlaw` (otherwise pass `--repo-dir` before the command, or set `REG_REPO_DIR`):

```bash
python runner.py sync --branch 2026              # git fetch origin, print the latest commit
python runner.py catalog --branch 2026           # read the test list of origin/2026
python runner.py select                          # what would run for changes since last tested
python runner.py run --tests "header.bombay_hdr layout" --profile local
python runner.py run --all                       # one process, no Buildbot, no shards (uses the folder as it is)
```

The original command line of `indianlaw` is unchanged and does not use the CI: `python regression.py -P local -m layout -t analyze_paras`, and `-p` to write or refresh an expected file.

Run this repository's own tests in the Buildbot environment (it has Django and argon2):

```bash
cd ~/buildbotci
python manage.py test ciweb                                   # the web UI and the accounts
PYTHONPATH=. python -m unittest discover -s hub/tests -t .    # runner, git handling, store, diffs
```

The contract tests need the project's own Python, so run them there: `python -m unittest hub.tests.test_contract`. They are skipped when `indianlaw` is not next to this folder (set `REG_REPO_DIR`) or its branch has no `regression.MODULES`. The web tests use a temporary SQLite database; they never touch Postgres.
