# Regression CI

## What it is, and why

`indianlaw` has a large regression test suite. Running it by hand is slow, and nobody knows what broke until someone runs it. This repository is the CI that does it for them:

- **Every night** it checks the watched branches (`main`, `2026`) and, if there is a new commit, runs all the tests.
- **On demand**, a developer opens the web UI, picks tests, and clicks **Queue run**.
- Tests are split by module and run in parallel on several workers.
- The web UI shows live progress, logs, and a diff of expected against actual output for every failed test.

It is built from three parts:

| Part | What it is |
|---|---|
| **Buildbot master** | Schedules the nightly check, queues runs, hands each module to a free worker |
| **Buildbot workers** | Check out the commit and run the tests of one module at a time |
| **Web UI** (`ciweb/`, Django) | Pick tests, start and stop runs, read results. Sign-in is needed to start a run; reading needs none |

The CI never changes your `indianlaw` working copy. It runs `git fetch origin` there and tests throwaway `git worktree` checkouts. Nothing in `indianlaw` refers to this repository.

How it works inside (run flow, contract with `indianlaw`, all settings, accounts, data layout): see `CLAUDE.md`.

## Folders

```
~/
  buildbotci/     this repository
  indianlaw/      your clone of the application repository, remote named origin
  .buildbotci/    Buildbot virtualenv, master/ and worker folders (created below)
```

`git fetch origin` in `~/indianlaw` must work without a password (SSH key or deploy key).

## Local setup

For one machine: one master, three workers, and the web UI, all started by hand.

**Needs:** Python 3, git, PostgreSQL, the `indianlaw` clone above, and the project's own Python environment for the tests (for example `~/.indianlaw/bin/python`).

**1. Database** (once; as the Postgres admin, `sudo -u postgres psql`)

One database holds everything, with one schema for each program:

| Name | What |
|---|---|
| role `ci` | the database login used by both programs |
| database `cidb` | the one database |
| schema `buildbot` | Buildbot's tables (the master) |
| schema `ciweb` | the web UI's tables (accounts, sessions) |

```sql
CREATE ROLE ci LOGIN PASSWORD 'CHANGE-ME';
CREATE DATABASE cidb OWNER ci;
\c cidb
CREATE SCHEMA buildbot AUTHORIZATION ci;
CREATE SCHEMA ciweb    AUTHORIZATION ci;
```

Make the password with `python3 -c "import secrets; print(secrets.token_hex(12))"` and use the same one in the two URLs below.

**2. Buildbot environment**

```bash
python3 -m venv ~/.buildbotci
. ~/.buildbotci/bin/activate
pip install -r ~/buildbotci/requirements.txt
```

**3. Settings file `~/bb-master.env`**

One `KEY='value'` per line. Put a comment on a line of its own, never after a value: the web UI and systemd read a trailing comment as part of the value. Write paths as `'$HOME/...'`. Leave out `export` for a server (systemd ignores those lines); it works for local runs.

```bash
# database: user ci, database cidb. The part after ? makes Buildbot use the buildbot schema
BUILDBOT_DB_URL='postgresql+psycopg2://ci:CHANGE-ME@localhost/cidb?options=-csearch_path%3Dbuildbot'
BUILDBOT_WORKER_PASSWORD='CHANGE-ME'
BUILDBOT_ADMIN_PASSWORD='CHANGE-ME'
BUILDBOT_UI_PASSWORD='CHANGE-ME'
BUILDBOT_URL='http://localhost:8010/'
REG_WORKERS='w1,w2,w3'
REG_CI_DIR='$HOME/buildbotci'
REG_PYTHON='$HOME/.indianlaw/bin/python'
REG_RESULTS_DIR='$HOME/regression-results'
REG_WORKERS_DIR='$HOME/.buildbotci'
# which profile (.env.<profile>) indianlaw uses
REGRESSION_PROFILE='local'
# optional: a private file that overrides settings such as DATADIR from .env.local
REG_PROFILE_FILE='$HOME/.buildbotci/private/profile.env'
REG_BRANCHES='main,2026'
REG_CRON_MINUTE='0'
REG_CRON_HOUR='21'
TZ='Asia/Kolkata'

REG_UI_SECRET='CHANGE-ME-long-random-value'
REG_UI_DB_SCHEMA='ciweb'
# same user, password and database as above, without the ?options part
REG_UI_DB_URL='postgresql+psycopg2://ci:CHANGE-ME@localhost/cidb'
REG_UI_URL='http://127.0.0.1:8020'
REG_BUILDBOT_URL='http://localhost:8010/'
REG_BUILDBOT_USER='regression-ui'

REG_MAX_SHARDS='20'
BUILDBOT_WORKER_INTERFACE='127.0.0.1'
REG_MAX_PARALLEL_SHARDS='3'
```

Fill every `CHANGE-ME` with your own value, and keep the file private (`chmod 600 ~/bb-master.env`). Make the secret with `python3 -c "import secrets; print(secrets.token_hex(32))"`. The paths, `REG_WORKERS`, `REG_BRANCHES`, `REGRESSION_PROFILE`, `BUILDBOT_URL` and `REG_BUILDBOT_URL` have no default: the master, the runner and the web UI stop with an error that names the missing one. The other settings are listed in `CLAUDE.md`. Do not set `REG_UI_SECURE_COOKIES` or `REG_UI_BEHIND_PROXY` locally: the UI runs over plain HTTP.

**4. Private profile** (only if `.env.local` must be overridden)

By default the worker uses the `DATADIR` that `.env.local` already sets. To override it on this machine, create the file named in `REG_PROFILE_FILE`:

```bash
mkdir -p ~/.buildbotci/private
echo 'DATADIR=/home/barath-kumar/indianlaw_data' > ~/.buildbotci/private/profile.env
```

Use your own path. The file stays outside the repository, so it is never committed. If you don't need it, leave `REG_PROFILE_FILE` out: the profile step is then skipped.

**5. Master**

```bash
set -a; . ~/bb-master.env; set +a
buildbot create-master --db "$BUILDBOT_DB_URL" ~/.buildbotci/master
cp ~/buildbotci/hub/buildbot/master.cfg ~/.buildbotci/master/master.cfg
buildbot checkconfig ~/.buildbotci/master && buildbot start ~/.buildbotci/master
```

`buildbot create-master` makes Buildbot's tables inside the `buildbot` schema. Buildbot's own page is at http://localhost:8010.

**6. Workers** (three, `w1` to `w3`, matching `REG_WORKERS`)

```bash
set -a; . ~/bb-master.env; set +a
for i in 1 2 3; do
  buildbot-worker create-worker ~/.buildbotci/w$i localhost:9989 w$i "$BUILDBOT_WORKER_PASSWORD"
done
for i in 1 2 3; do
  echo "CI admin <admin@indiankanoon.com>" > ~/.buildbotci/w$i/info/admin
  echo "CI worker w$i" > ~/.buildbotci/w$i/info/host
  buildbot-worker start ~/.buildbotci/w$i
done
```

The two `info/` lines are the name and description Buildbot's web page shows for each worker. The worker needs `git`, `REG_PYTHON`, the data directory (`DATADIR`, which must exist) and write access to `REG_RESULTS_DIR`.

**7. Web UI**

```bash
cd ~/buildbotci
python manage.py migrate            # creates the account tables in the ciweb schema
python manage.py createsuperuser    # your administrator login
python manage.py runserver          # http://127.0.0.1:8020
```

`manage.py` reads `~/bb-master.env` by itself. `runserver` reloads when you edit code, templates or the style sheet. It is for your own machine only.

Sign in, open the start page and press **Refresh from GitLab** once per branch so the test picker has a list. Then queue a run.

**Stop and restart**

```bash
buildbot stop ~/.buildbotci/master
for i in 1 2 3; do buildbot-worker stop ~/.buildbotci/w$i; done
```

After you change something:

| You changed | Do this |
|---|---|
| `~/bb-master.env` | Stop and start the program that reads it |
| `hub/buildbot/master.cfg` | `cp` it to `~/.buildbotci/master/`, then `buildbot checkconfig ~/.buildbotci/master && buildbot reconfig ~/.buildbotci/master` |
| Web UI code | Nothing with `runserver`. Reload the browser |
| `runner.py` | Nothing. Each run reads it fresh |

**Tests of this repository**

```bash
cd ~/buildbotci
python manage.py test ciweb
PYTHONPATH=. python -m unittest discover -s hub/tests -t .
```

## Server setup

The same parts, kept running by systemd, with HTTPS in front of the UI. Everything needed is in `deploy/`. Nothing in it is installed by itself: copy a file, edit it, install it.

| File | Use |
|---|---|
| `deploy/bb-master.env.example` | The full settings file for a server (HTTPS, SMTP, limits) |
| `deploy/systemd/buildbot-master.service` | Runs the master |
| `deploy/systemd/buildbot-worker@.service` | Runs one worker per instance (`@w1`, `@w2`, ...) with a memory limit |
| `deploy/systemd/regression-ui.service` | Runs the web UI with gunicorn through `bin/run-ui` |
| `deploy/nginx/regression-ci.conf` | HTTPS in front of the UI |

**1. Prepare the machine.** Do local-setup steps 1 (database), 2, 5 (`create-master` and `master.cfg` only) and 6 (`create-worker` only) as the account that will run the CI. Create one worker folder per worker (`w1`, `w2`, `w3`).

**2. Settings file.**

```bash
cp ~/buildbotci/deploy/bb-master.env.example ~/bb-master.env && chmod 600 ~/bb-master.env
```

Fill in every `CHANGE-ME` and `<...>` value. List all workers in `REG_WORKERS`. Set `REG_UI_URL` to the real `https://` address.

**3. Edit the systemd units.** They are set to the account `sushant` and `/home/sushant/...`. For another account, replace those in all three files. systemd does not expand `$HOME`, and `%h` means `/root` in system units, so use absolute paths.

**4. Install and start.**

```bash
sudo cp ~/buildbotci/deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now buildbot-master
sudo systemctl enable --now buildbot-worker@w1 buildbot-worker@w2 buildbot-worker@w3
sudo systemctl enable --now regression-ui
```

Match the `buildbot-worker@...` instances to `REG_WORKERS`. Size the worker memory limit as `(total RAM - 2 GB) / number of workers`. Check with `systemctl status buildbot-master` and `journalctl -u buildbot-master -n 50`. The first time, run `python manage.py createsuperuser` in `~/buildbotci`.

**5. HTTPS.** Edit the host name and certificate paths in `deploy/nginx/regression-ci.conf`, put it in nginx's config folder, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

In `~/bb-master.env` set `REG_UI_BEHIND_PROXY='1'` and `REG_UI_SECURE_COOKIES='1'`, and restart `regression-ui`. Once HTTPS works, set `REG_UI_HSTS_SECONDS='31536000'` and `REG_UI_SSL_REDIRECT='1'`. Without HTTPS, passwords cross the network in clear text.

**More worker machines.** Each needs this repository and `indianlaw` at the same paths as on the master, the worker unit, and a worker folder created with `buildbot-worker create-worker ... <master-host>:9989 <name> "$BUILDBOT_WORKER_PASSWORD"`. Add its name to `REG_WORKERS` on the master. All workers and the UI must see the same `REG_RESULTS_DIR` (same host or a shared mount).

**Updating**

```bash
cd ~/buildbotci && git pull
bin/apply-update          # installs requirements, migrates, reloads the UI without dropping requests
```

For `master.cfg`, copy it to the master folder and run `buildbot reconfig` (running builds are kept). After editing `~/bb-master.env`, restart the service that reads it.

**Housekeeping (the `bin/` scripts)**

All of these load `~/bb-master.env` themselves. Run them from `~/buildbotci` as the CI account.

| Command | What it does |
|---|---|
| `bin/prune-runs --days 30` | Lists runs older than 30 days. Add `--yes` to delete them. Put it in cron, nothing prunes runs by itself |
| `bin/clean-worktrees` | Removes build checkouts left by crashed builds. `--all` only when Buildbot is stopped |
| `bin/clear-queue` | Shows unfinished work the master would resume after a crash. `--yes` cancels it. Stop the master first |
