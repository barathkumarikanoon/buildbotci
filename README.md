# Indian Kanoon CI

## What it is

Our applications have test suites that are slow to run by hand, and nobody knows what broke until someone runs them. This repository is the CI that runs them for us. It tests **any number of repositories**, each described once, in the website, as a *repo profile*:

- **Automatically**, at a time you set for each repository, it checks the branches you mark and, if there is a new commit, runs all the tests.
- **On demand**, a developer opens a repository, clicks a branch, picks tests and clicks **Queue run**.
- Tests are split by module and run in parallel on the repository's own workers (three by default).
- The website shows live progress, logs and results, kept separately for each repository and branch. Suites that compare output with an expected file also show the exact difference.

Each repository says how its tests are found and judged (its *test suite type*):

| Type | For | A test passes when |
|---|---|---|
| **Output comparison** | `indianlaw` (the Indian Kanoon regression framework) | its output equals `regressions/expected/<name>` |
| **pytest (Python tests)** | a suite run by pytest, for example `legal-chatbot` | pytest says so |
| **Command checks** | anything runnable from a shell, for example `egazette` | the command exits as expected, and, if you ask for it, prints what a golden file says |

A new type is one small class (`CLAUDE.md`, "Adding a new type of test suite").

It is made of three parts:

| Part | What it does |
|---|---|
| **Buildbot master** | Creates the builders, schedules and workers of every repo profile by itself, queues runs, hands each module to a free worker |
| **Buildbot workers** | Check out the commit and run the tests of one module at a time. Every repository has its own workers |
| **Website** (`ciweb/`, Django) | Add and edit repositories, pick tests, start and stop runs, read results. Starting a run needs an account; reading needs none. Administrators manage repositories and people |

The CI never changes a repository's working copy. It runs `git fetch origin` in the clone you name and tests throwaway `git worktree` checkouts. Nothing in the tested repositories refers to this one.

How it works inside (run flow, repo profiles, test suite types, every setting, accounts, data layout) is in `CLAUDE.md`.

## Folders

```
~/
  buildbotci/     this repository
  indianlaw/      a clone of a tested repository, remote named origin (one clone per repo profile)
  egazette/
  .buildbotci/    Buildbot virtual environment, master/ and the worker folders (made below)
  ci-results/     results of every repository, one folder each (made by the CI)
```

`git fetch origin` in each clone must work without a password (an SSH key or a deploy key).

## Local setup

One machine: the master, the workers of each repository and the website, all started by you.

**You need:** Python 3, git, PostgreSQL, a clone of each repository to test, and the Python environment each repository's tests need (for example `~/.indianlaw/bin/python`).

**1. Database** (once, as the Postgres administrator: `sudo -u postgres psql`)

One database holds everything, with one schema for each program:

| Name | What |
|---|---|
| role `ci` | the database login both programs use |
| database `cidb` | the one database |
| schema `buildbot` | Buildbot's tables (the master) |
| schema `ciweb` | the website's tables (accounts, sessions, repo profiles) |

```sql
CREATE ROLE ci LOGIN PASSWORD 'CHANGE-ME';
CREATE DATABASE cidb OWNER ci;
\c cidb
CREATE SCHEMA buildbot AUTHORIZATION ci;
CREATE SCHEMA ciweb    AUTHORIZATION ci;
```

Make the password with `python3 -c "import secrets; print(secrets.token_hex(12))"` and use the same one in both database addresses below.

**2. Buildbot environment**

```bash
python3 -m venv ~/.buildbotci
. ~/.buildbotci/bin/activate
pip install -r ~/buildbotci/requirements.txt
```

**3. Settings file `~/bb-master.env`**

One `KEY='value'` per line. Put a comment on a line of its own, never after a value: the website and systemd read a trailing comment as part of the value. Write paths as `'$HOME/...'`. Leave out `export` on a server (systemd ignores those lines); locally it works either way.

```bash
# database: role ci, database cidb. The part after ? makes Buildbot use the buildbot schema
BUILDBOT_DB_URL='postgresql+psycopg2://ci:CHANGE-ME@localhost/cidb?options=-csearch_path%3Dbuildbot'
BUILDBOT_WORKER_PASSWORD='CHANGE-ME'
BUILDBOT_ADMIN_PASSWORD='CHANGE-ME'
BUILDBOT_UI_PASSWORD='CHANGE-ME'
BUILDBOT_URL='http://localhost:8010/'
CI_DIR='$HOME/buildbotci'
CI_RESULTS_DIR='$HOME/ci-results'
CI_WORKERS_DIR='$HOME/.buildbotci'
TZ='Asia/Kolkata'

CI_UI_SECRET='CHANGE-ME-long-random-value'
CI_UI_DB_SCHEMA='ciweb'
# same role, password and database as above, without the ?options part
CI_UI_DB_URL='postgresql+psycopg2://ci:CHANGE-ME@localhost/cidb'
CI_UI_URL='http://127.0.0.1:8020'
CI_BUILDBOT_URL='http://localhost:8010/'
CI_BUILDBOT_USER='ci-ui'
BUILDBOT_WORKER_INTERFACE='127.0.0.1'
```

Replace every `CHANGE-ME` with your own value and keep the file private (`chmod 600 ~/bb-master.env`). Make the secret with `python3 -c "import secrets; print(secrets.token_hex(32))"`. The paths, `BUILDBOT_URL` and `CI_BUILDBOT_URL` have no default: the master, the runner and the website stop with an error that names the missing one. All other settings are in `CLAUDE.md`. Do not set `CI_UI_SECURE_COOKIES` or `CI_UI_BEHIND_PROXY` locally: the website runs over plain HTTP there.

Settings are named `CI_...` (or `BUILDBOT_...`).

**Making both programs use this file.** One file serves the master and the website, and the variable `CI_ENV` says which one. In **every terminal** you start the master, the workers or the website from, run this first:

```bash
export CI_ENV="$HOME/bb-master.env"     # use your own file name here
echo $CI_ENV                            # check it
```

The setting lasts until you close that terminal; nothing is written to `~/.bashrc` or any other file, so type it again in each new terminal. Without it, everything falls back to `~/bb-master.env`. For one command only, put it in front: `CI_ENV=$HOME/bb-master.env bin/master status`.

- The master and the workers are started with the scripts in `bin/` (`bin/master`, `bin/workers`, ...). They load `$CI_ENV` themselves.
- The website (`python manage.py ...`, `runserver`) reads the same file by itself.
- You never `source` the file or export single settings by hand (the one-time `create-master` in step 4 is the only exception). A setting already exported in a terminal beats the file for the website, which is how the master and the website end up with different accounts and a "login failed (HTTP 401)".
- After you edit the file, restart the master and the website. They only read it when they start.
- To check that both agree:

```bash
tr '\0' '\n' < /proc/$(cat ~/.buildbotci/master/twistd.pid)/environ | grep -E '^CI_(RESULTS_DIR|BUILDBOT_USER)='
python manage.py shell -c "from django.conf import settings; print(settings.RESULTS_DIR, settings.BUILDBOT_USER)"
```

Both lines must show the same results folder and the same account name.

On a server you do not export it by hand: systemd starts the programs, so the units name the file (see "Server setup", step 3).

Nothing about one particular repository goes in this file. Its folder, Python, branches, schedule, workers and test suite type are entered in the website (step 6).

**4. Master**

```bash
set -a; . "${CI_ENV:-$HOME/bb-master.env}"; set +a     # only for this one-time command
buildbot create-master --db "$BUILDBOT_DB_URL" ~/.buildbotci/master   # once: makes the tables in the buildbot schema
cd ~/buildbotci
bin/master update       # copies master.cfg, checks it, starts the master
```

Buildbot's own page is at http://localhost:8010. Until the first repository exists the master runs with a stand-in builder; adding a repository needs no restart and no new `master.cfg`. `bin/master status`, `stop`, `restart` and `check` do what they say.

**5. Website**

```bash
cd ~/buildbotci
python manage.py migrate            # creates the tables in the ciweb schema
python manage.py createsuperuser    # your administrator login
python manage.py runserver          # http://127.0.0.1:8020
```

`manage.py` reads `~/bb-master.env` by itself. `runserver` reloads when you change code, templates or the style sheet. It is for your own machine only.

**6. Add your first repository** (in the browser)

Sign in as the administrator, open **Manage repositories** and press **Add a repository**. For `indianlaw`, for example:

| Field | Value |
|---|---|
| Key / Name | `indianlaw` / `Indian Law` |
| Clone folder | `$HOME/indianlaw` |
| Checkout name | `indianlaw` (its code imports itself by that name) |
| Python | `$HOME/.indianlaw/bin/python` |
| Test suite type | Output comparison, environment profile `local` (and, if needed, a private settings file such as one with `DATADIR=...`) |
| Branches | `main`, `2026`; tick the ones tested automatically |
| Workers | 3 (named `indianlaw-w1` to `indianlaw-w3`) |

For a repository without a test framework of its own, such as `egazette`, choose **Command checks** and list the checks one per line (`name | command`). For a pytest suite such as `legal-chatbot`, choose **pytest (Python tests)** and give the paths. Each repository can use its own Python.

Coming from the time before repo profiles? `python manage.py ci_repos seed-legacy` makes the `indianlaw` profile from the old settings (`CI_BRANCHES`, `CI_PYTHON`, `CI_WORKERS`, ...) and moves the old results into their new folder. Afterwards remove those lines from `~/bb-master.env`.

Saving writes the repository to Buildbot within seconds (the page says when Buildbot has it). Running builds are not interrupted.

**7. Workers** (every repository has its own)

Saving a repository in the website makes its worker folders on this machine **and starts the workers**; the page says what it did. For a repository that already exists, press **Create and start workers** on its page (the **Workers** card shows each worker as Connected, Busy, Not connected or Not created). From a shell:

```bash
cd ~/buildbotci
bin/workers start indianlaw      # or no name: every worker here
bin/workers status               # "running, connected to the master"
```

`bin/create-workers` makes the folders from a shell, for a worker machine other than the website's, or after a warning on the save. Raising a repository's number of workers handles the new ones the same way. Started this way, workers get none of the CI's settings (no passwords): the master hands each build the few it needs. A worker needs `git`, the repository's Python, its data (for `indianlaw`, `DATADIR` must exist) and write access to `CI_RESULTS_DIR`. On a server with systemd the website cannot start units without sudo, so it shows the `sudo systemctl enable --now buildbot-worker@NAME` line to run; workers on another machine than the website are made and started there (set `CI_AUTO_CREATE_WORKERS=0` and `CI_AUTO_START_WORKERS=0`).

Open the repository in the website and press **Refresh test list** once for each branch, so the test picker has a list. Then queue a run.

**Stopping**

```bash
bin/workers stop          # all workers (one that is running a build is skipped; --force stops it anyway)
bin/master stop
```

**After you change something**

| You changed | Do this |
|---|---|
| A repository in the website | Nothing. The master follows within seconds, and the website makes and starts any new workers |
| `~/bb-master.env` | Restart the program that reads it |
| `hub/buildbot/master.cfg` | `bin/master update` |
| `hub/repos.py`, `hub/adapters/`, `hub/buildbot/repo_config.py` | `bin/master reconfig` |
| Other code under `hub/`, `runner.py` | Nothing: every run reads it fresh |
| Website code | Nothing with `runserver`; reload the browser |

**Tests of this repository**

```bash
cd ~/buildbotci
python manage.py test ciweb
PYTHONPATH=. python -m unittest discover -s hub/tests -t .
```

## Server setup

The same parts, kept running by systemd, with HTTPS in front of the website. Everything needed is in `deploy/`. Nothing in it installs itself: copy a file, edit it, install it.

| File | Use |
|---|---|
| `deploy/bb-master.env.example` | The full settings file for a server (HTTPS, email) |
| `deploy/systemd/buildbot-master.service` | Runs the master |
| `deploy/systemd/buildbot-worker@.service` | Runs one worker per instance (`@indianlaw-w1`, `@egazette-w1`, ...) with a memory limit |
| `deploy/systemd/ci-ui.service` | Runs the website with gunicorn through `bin/run-ui` |
| `deploy/nginx/ci.conf` | HTTPS in front of the website |

The master and the website need the database, so their units `Require` and start after the Postgres unit `postgresql@localdbs.service`, and `bin/wait-for-db` holds their start until the database accepts logins. If your cluster instance has another name (`systemctl list-units 'postgresql@*'`), change it in those two files. The CI never installs or edits the Postgres unit itself. Workers talk only to the master, so they depend on the master and not on Postgres.

**1. Prepare the machine.** Do local steps 1 (database), 2 and the `create-master` line of step 4 as the account that runs the CI.

**2. Settings file.**

```bash
cp ~/buildbotci/deploy/bb-master.env.example ~/bb-master.env && chmod 600 ~/bb-master.env
```

Fill in every `CHANGE-ME` and `<...>` value. Set `CI_UI_URL` to the real `https://` address.

**3. Edit the units.** They are written for the account `sushant` and `/home/sushant/...`. For another account, replace those in all three files. systemd does not expand `$HOME`, and `%h` means `/root` in system units, so write absolute paths.

Name the settings file in the units, not with `export` (a service does not see what you type in a terminal):

- `buildbot-master.service`: `EnvironmentFile=/home/<account>/bb-master.env`
- `ci-ui.service`: `Environment=CI_ENV=/home/<account>/bb-master.env` (so `bin/run-ui` reads the same file)

Use the real file name in both, owned by the service account with mode 600 (it holds the passwords). After you edit the file, run `sudo systemctl restart buildbot-master ci-ui`. The workers do not load it.

**4. Install and start.**

```bash
sudo cp ~/buildbotci/deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now buildbot-master
cd ~/buildbotci && python manage.py migrate && python manage.py createsuperuser     # once: the tables, then your administrator
sudo systemctl enable --now ci-ui
```

Then add your repositories in the website (local step 6), the website makes the worker folders when you save a repository (`bin/create-workers` does it from a shell), then start them, one instance each:

```bash
sudo systemctl enable --now buildbot-worker@indianlaw-w1 buildbot-worker@indianlaw-w2 buildbot-worker@indianlaw-w3
```

`enable` makes them start at boot. `bin/workers` and `bin/master` use systemd on their own when the units are installed. Size the worker memory limit as `(total memory - 2 GB) / number of workers`. Check with `systemctl status buildbot-master` and `journalctl -u buildbot-master -n 50`.

**5. HTTPS.** Edit the host name and certificate paths in `deploy/nginx/ci.conf`, put it in nginx's configuration folder, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

In `~/bb-master.env` set `CI_UI_BEHIND_PROXY='1'` and `CI_UI_SECURE_COOKIES='1'`, and restart `ci-ui`. When HTTPS works, set `CI_UI_HSTS_SECONDS='31536000'` and `CI_UI_SSL_REDIRECT='1'`. Without HTTPS, passwords cross the network in clear text.

**More worker machines.** Each needs this repository and the clones it serves at the same paths as on the master (a repository's clone folder and Python are the same on every machine), the worker unit, and a worker folder made with `buildbot-worker create-worker ... <master-host>:9989 <name> "$BUILDBOT_WORKER_PASSWORD"`. Name the workers as the repo profile does (the repository's edit page, "Worker names"). All workers and the website must see the same `CI_RESULTS_DIR` (the same machine or a shared mount), because the repo profiles file lives in it. Workers on other machines are started, stopped and deleted on those machines.

**Updating**

```bash
cd ~/buildbotci && git pull
bin/apply-update        # installs requirements, migrates, reloads the website without dropping requests
bin/master update       # only if hub/buildbot/master.cfg changed (running builds are kept)
```

After you edit `~/bb-master.env`, restart the service that reads it.

## The `bin/` scripts

**Who may run them.** A script that changes something asks for the user name and password of a website **superuser** (an account made with `python manage.py createsuperuser`, or one an administrator made a superuser) and checks them against the website's accounts. You log in **once for each terminal**; the scripts after that do not ask again:

```bash
bin/login                    # asks for the name and password once
bin/master update            # no question
bin/workers start
bin/login --who              # who is logged in here
bin/logout                   # end it (also ends by itself: 30 minutes unused, 8 hours, terminal closed)
```

Running a script first, without `bin/login`, asks the same way and starts the login. The login also ends if the account is disabled, loses its superuser right or changes its password; `CI_LOGIN_MINUTES` sets the idle time (`0` asks every time). Looking is free: `status`, `check` and every dry run (without `--yes`) ask nothing, and neither do `bin/run-ui` and `bin/wait-for-db`, which systemd runs. Before the first superuser exists nothing is asked, so local setup works in order. Without a terminal (cron) set `CI_ADMIN_USER` and `CI_ADMIN_PASSWORD` for that job. Every use is logged to `$CI_RESULTS_DIR/admin-actions.log`. Details: `CLAUDE.md`, "Who may run the scripts".

All load `~/bb-master.env` by themselves and build every path from `$HOME`. Run them from `~/buildbotci` as the CI account. Those that act on runs, checkouts or workers take a repository key (`KEY`) or `--repo KEY`; without one they cover every repository.

| Command | What it does |
|---|---|
| `bin/master update` | Copies `master.cfg` into the master folder, checks it, then starts the master, or reloads it when it already runs (builds are kept). Also `check`, `start`, `stop`, `restart`, `reconfig`, `status` |
| `bin/login`, `bin/logout` | Log in once for this terminal as a website superuser, so the scripts below that change something do not ask again; end it |
| `bin/create-workers [KEY]` | Makes the worker folders that do not exist yet on this machine and says how to start them |
| `bin/workers start\|stop\|restart\|status [KEY] [--worker NAME]` | Starts, stops, restarts or shows the workers on this machine: all, one repository's, or chosen ones. A worker running a build is not stopped unless `--force` |
| `bin/delete-workers [KEY] [--worker NAME] [--orphans]` | Stops and deletes chosen workers on this machine; the repository stays. Shows what it would do; `--yes` does it |
| `bin/run-ui` | Migrates, collects static files and starts the website with gunicorn (what `ci-ui.service` runs) |
| `bin/apply-update` | Installs requirements, migrates, reloads the website |
| `bin/wait-for-db [SECONDS]` | Waits until the database accepts logins (run by the units before they start) |
| `bin/prune-runs --days 30` | Lists runs older than 30 days; `--yes` deletes them. Put it in cron: nothing prunes runs by itself |
| `bin/clean-worktrees` | Removes build checkouts that crashed builds left behind; `--all` only while Buildbot is stopped |
| `bin/clear-queue` | Shows unfinished work the master would resume after a crash; `--yes` cancels it. Stop the master first |

Deleting a whole repository (its profile, workers and, if asked, its results) is **Manage repositories → the repository → Delete…** in the website, or `python manage.py ci_repos remove KEY`.
