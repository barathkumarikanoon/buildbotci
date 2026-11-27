"""Repo profiles: the one description of "a repository the CI tests".

A repo profile says where the code is, which Python environment runs it, how its tests are found and judged
(the *adapter*), which branches to watch, when to run them by themselves, and which Buildbot workers serve it.

The web UI keeps the profiles in its database and writes them to one JSON file (the *snapshot*, see `save_snapshot`).
The Buildbot master and the runner on the workers read only that file, so none of them needs Django or the database.
Everything here is standard library only, because the master runs in Buildbot's own environment.
"""
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field

KEY_RE        = re.compile(r'^[a-z][a-z0-9-]{1,31}$')
SCHEDULER_NAME_MAX = 70
# Test suite types that were renamed: the old key is still read (in the database, in profiles.json, in old run records).
ADAPTER_ALIASES = {'indianlaw': 'comparison'}
BRANCH_RE     = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$')
WORKER_RE     = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$')
ENV_NAME_RE   = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
CRON_PART_RE  = re.compile(r'^(\*|\d{1,2}(-\d{1,2})?)(/\d{1,2})?(,(\*|\d{1,2}(-\d{1,2})?)(/\d{1,2})?)*$')
SNAPSHOT_NAME = 'profiles.json'
APPLIED_NAME  = 'profiles.applied.json'
FORMAT        = 1


def expand_path(value):
    """A folder or file written as ~/x, $HOME/x or an absolute path."""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(value))) if value else ''


def expand_command(value):
    """A program written as a path (`~/x/bin/python`, `$HOME/x/bin/python`) gets its real path, because nothing that starts it
    (Buildbot, subprocess) uses a shell to expand it. A bare name (`python3`) is looked up on PATH, so it stays as it is."""
    if value and ('/' in value or value.startswith('~') or '$' in value):
        return os.path.expanduser(os.path.expandvars(value.strip()))
    return value.strip() if value else value


def default_snapshot_path(results_root = None):
    """Where the snapshot is: CI_PROFILES_FILE, else `profiles.json` inside the results folder."""
    explicit = os.environ.get('CI_PROFILES_FILE')
    if explicit:
        return expand_path(explicit)
    root = results_root or os.environ.get('CI_RESULTS_DIR')
    return os.path.join(expand_path(root), SNAPSHOT_NAME) if root else ''


def applied_path(snapshot_path):
    return os.path.join(os.path.dirname(snapshot_path), APPLIED_NAME)


def branch_slug(name):
    """A branch name as part of a Buildbot name: Buildbot allows letters, digits, `_` and `-` only."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', name)


@dataclass
class BranchConfig:
    name:        str
    scheduled:   bool = True
    cron_hour:   str = ''          # when this branch is tested by itself; empty = the repo's own time
    cron_minute: str = ''


@dataclass
class RepoConfig:
    key:           str
    name:          str
    adapter:       str
    repo_dir:      str
    description:   str = ''
    enabled:       bool = True
    options:       dict = field(default_factory = dict)      # settings of the adapter, see hub/adapters
    checkout_name: str = ''                                  # folder name of a checkout; default: the name of repo_dir
    python:        str = 'python3'                           # interpreter of this repo's own environment
    env:           dict = field(default_factory = dict)      # environment variables the tests run with
    branches:      list = field(default_factory = list)      # [BranchConfig]
    schedule:      bool = True
    cron_hour:     str = '21'
    cron_minute:   str = '0'
    workers:       list = field(default_factory = list)      # names of the Buildbot workers that serve this repo
    max_runs:      int = 8                                   # runs of this repo in progress at once
    max_shards:    int = 32                                  # module builds of one run
    max_parallel:  int = 0                                   # module builds at once, 0 = as many as there are workers
    step_timeout:  int = 7200                                # seconds without output
    step_max_time: int = 21600                               # seconds in total

    def __post_init__(self):
        self.adapter       = ADAPTER_ALIASES.get(self.adapter, self.adapter)
        self.repo_dir      = expand_path(self.repo_dir)
        self.python        = expand_command(self.python)
        self.checkout_name = self.checkout_name or os.path.basename(self.repo_dir)
        self.branches      = [b if isinstance(b, BranchConfig) else BranchConfig(**b) for b in self.branches]

    @property
    def branch_names(self):
        return [b.name for b in self.branches]

    @property
    def scheduled_branches(self):
        return [b.name for b in self.branches if b.scheduled] if self.schedule else []

    def branch_config(self, name):
        return next((b for b in self.branches if b.name == name), None)

    def branch_cron(self, name):
        """(hour, minute) of the nightly run of a branch: its own time, else the repo's."""
        branch = self.branch_config(name)
        return ((branch.cron_hour if branch and branch.cron_hour != '' else self.cron_hour),
                (branch.cron_minute if branch and branch.cron_minute != '' else self.cron_minute))

    # The names Buildbot knows this repo's parts by. The master, the web UI and the runner all take them from here.
    @property
    def run_builder(self):
        return '%s-run' % self.key

    @property
    def shard_builder(self):
        return '%s-shard' % self.key

    @property
    def catalog_builder(self):
        return '%s-catalog' % self.key

    def force_scheduler_for(self, branch):
        """The scheduler that starts a run of one branch on request (tests: all or a selection)."""
        return 'force-%s-%s' % (self.key, branch_slug(branch))

    @property
    def catalog_scheduler(self):
        return 'catalog-%s' % self.key

    def nightly_scheduler(self, branch):
        """The scheduler that tests a branch by itself at its time (always every test)."""
        return '%s-nightly-%s' % (self.key, branch_slug(branch))

    def shard_scheduler(self, index):
        return '%s-shard-%d' % (self.key, index)

    @property
    def nightly_prefix(self):
        return '%s-nightly-' % self.key

    def results_dir(self, root):
        return os.path.join(expand_path(root), self.key)

    def to_dict(self):
        data = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != 'branches'}
        data['branches'] = [{'name': b.name, 'scheduled': b.scheduled, 'cron_hour': b.cron_hour, 'cron_minute': b.cron_minute}
                            for b in self.branches]
        return data

    @classmethod
    def from_dict(cls, data):
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def validate_cron(value, low, high):
    """A cron field such as `*`, `21`, `0,30`, `*/15` or `9-17`; returns an error text or None."""
    value = str(value).strip()
    if not CRON_PART_RE.match(value):
        return 'use cron syntax such as 21, 0,30, */15 or *'
    for number in re.findall(r'\d+', value.replace('*/', '')):
        if not low <= int(number) <= high:
            return 'numbers must be between %d and %d' % (low, high)
    return None


def validate(config, taken_workers = ()):
    """Problems that would make the profile unusable, as a list of (field, message). `taken_workers` are the
    workers of the other profiles: a worker can serve one repo only."""
    problems = []
    if not KEY_RE.match(config.key or ''):
        problems.append(('key', 'Use 2 to 32 characters: lowercase letters, digits and dashes, starting with a letter.'))
    if not config.name.strip():
        problems.append(('name', 'A name is required.'))
    if not config.repo_dir:
        problems.append(('repo_dir', 'The folder of the repository clone is required.'))
    if not config.python.strip():
        problems.append(('python', 'The Python of the repository environment is required.'))
    if not config.branches:
        problems.append(('branches', 'Add at least one branch to test.'))
    seen = set()
    for branch in config.branches:
        if not BRANCH_RE.match(branch.name) or '..' in branch.name:
            problems.append(('branches', 'Not a usable branch name: %r' % branch.name))
        if branch_slug(branch.name) in seen:
            problems.append(('branches', 'Branch %s is listed twice, or is too like another branch (Buildbot names its schedulers after the branch, with every character other than letters, digits, - and _ written as _).' % branch.name))
        seen.add(branch_slug(branch.name))
        if max(len(config.nightly_scheduler(branch.name)), len(config.force_scheduler_for(branch.name))) > SCHEDULER_NAME_MAX:
            problems.append(('branches', 'The branch name %s is too long: Buildbot names its schedulers after it and allows %d characters.' % (
                branch.name, SCHEDULER_NAME_MAX)))
        for label, value, low, high in (('hour', branch.cron_hour, 0, 23), ('minute', branch.cron_minute, 0, 59)):
            error = value != '' and validate_cron(value, low, high)
            if error:
                problems.append(('branches', 'Branch %s, %s of its nightly run: %s.' % (branch.name, label, error)))
    if not config.workers:
        problems.append(('workers', 'At least one worker is required.'))
    for name in config.workers:
        if not WORKER_RE.match(name):
            problems.append(('workers', 'Not a usable worker name: %r' % name))
        if name in taken_workers:
            problems.append(('workers', 'Worker %s already serves another repository profile.' % name))
    if len(set(config.workers)) != len(config.workers):
        problems.append(('workers', 'A worker is listed twice.'))
    for name in config.env:
        if not ENV_NAME_RE.match(name):
            problems.append(('env', 'Not an environment variable name: %r' % name))
    for label, value, low, high in (('cron_hour', config.cron_hour, 0, 23), ('cron_minute', config.cron_minute, 0, 59)):
        error = validate_cron(value, low, high)
        if error:
            problems.append((label, error))
    for label in ('max_runs', 'max_shards', 'step_timeout', 'step_max_time'):
        if int(getattr(config, label)) < 1:
            problems.append((label, 'Must be at least 1.'))
    if int(config.max_parallel) < 0:
        problems.append(('max_parallel', 'Must be 0 or more.'))
    return problems


def parse_env_lines(text):
    """`KEY=value` lines (blank lines and lines starting with # are skipped) to a dict. Raises ValueError on a bad line."""
    values = {}
    for number, raw in enumerate((text or '').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        name, sep, value = line.partition('=')
        name = name.strip()
        if not sep or not ENV_NAME_RE.match(name):
            raise ValueError('line %d: write it as NAME=value' % number)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '\'"':
            value = value[1:-1]
        values[name] = value
    return values


def snapshot_text(configs):
    """The text of the snapshot for these profiles: stable (the same profiles give the same bytes) so that a
    save which changes nothing does not make the master reload."""
    ordered = sorted(configs, key = lambda c: c.key)
    return json.dumps({'format': FORMAT, 'repos': [c.to_dict() for c in ordered]}, indent = 1, sort_keys = True) + '\n'


def text_version(text):
    return hashlib.sha256(text.encode('utf8')).hexdigest()[:16]


def save_snapshot(path, configs):
    """Write the snapshot atomically and readable by its owner only (it may hold the environment of the tests).
    Returns True when the file changed."""
    text = snapshot_text(configs)
    try:
        with open(path, 'r', encoding = 'utf8') as handle:
            if handle.read() == text:
                return False
    except OSError:
        pass
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok = True)
    fd, tmp = tempfile.mkstemp(dir = directory, prefix = '.tmp-')
    with os.fdopen(fd, 'w', encoding = 'utf8') as handle:
        handle.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


def read_snapshot_text(path):
    try:
        with open(path, 'r', encoding = 'utf8') as handle:
            return handle.read()
    except OSError:
        return ''


def load_repos(path, only_enabled = True):
    """{key: RepoConfig} from the snapshot. A missing file is an empty list, not an error: nothing is configured yet."""
    text = read_snapshot_text(path)
    if not text.strip():
        return {}
    data = json.loads(text)
    repos = {}
    for item in data.get('repos', []):
        config = RepoConfig.from_dict(item)
        if config.enabled or not only_enabled:
            repos[config.key] = config
    return repos


def get_repo(path, key):
    repos = load_repos(path)
    if key not in repos:
        known = ', '.join(sorted(repos)) or 'none'
        raise SystemExit('No enabled repository profile named %r in %s (known: %s). Add it in the website under Repositories.' % (key, path, known))
    return repos[key]


def read_applied(path):
    """What the master reports about the snapshot it loaded: {'version', 'at', 'error'} or None."""
    try:
        with open(applied_path(path), 'r', encoding = 'utf8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


# ---- asking the master to erase the Buildbot history of deleted repos (hub/buildbot/cleanup.py) ----------------------

ERASE_NAME = 'cleanup.json'


def erase_request_path(snapshot_path):
    return os.path.join(os.path.dirname(snapshot_path), ERASE_NAME)


def read_erase_requests(path):
    try:
        with open(path, encoding = 'utf8') as handle:
            names = json.load(handle).get('erase_builders') or []
    except (OSError, ValueError, AttributeError):
        return []
    return sorted({n for n in names if isinstance(n, str)})


def write_erase_requests(path, names):
    """Replace the list of builder names whose history the master should erase (an empty list removes the file)."""
    names = sorted(set(names))
    if not names:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok = True)
    tmp = '%s.tmp-%d' % (path, os.getpid())
    with open(tmp, 'w', encoding = 'utf8') as handle:
        json.dump({'erase_builders': names}, handle)
    os.replace(tmp, path)
