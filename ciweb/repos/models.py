from django.core.validators import RegexValidator
from django.db import models

from hub import repos as hub_repos
from hub.adapters import ADAPTERS

KEY_VALIDATOR = RegexValidator(hub_repos.KEY_RE, 'Use 2 to 32 characters: lowercase letters, digits and dashes, starting with a letter.')
BRANCH_VALIDATOR = RegexValidator(hub_repos.BRANCH_RE, 'Not a usable branch name.')


def adapter_choices():
    return [(kind, cls.label) for kind, cls in ADAPTERS.items()]


class RepoProfile(models.Model):
    """One repository the CI tests: where it is, how its tests are found and judged, which branches, which workers."""
    key           = models.CharField(max_length = 32, unique = True, validators = [KEY_VALIDATOR],
                                     help_text = 'Short name used in addresses and folders, for example indianlaw. It cannot be changed later.')
    name          = models.CharField(max_length = 80, help_text = 'Shown in the site, for example Indian Law.')
    description   = models.CharField(max_length = 200, blank = True)
    enabled       = models.BooleanField(default = True, help_text = 'A disabled profile keeps its results but is not tested and cannot be run.')

    adapter       = models.CharField(max_length = 32, choices = adapter_choices, default = 'command', verbose_name = 'Test suite type')
    options       = models.JSONField(default = dict, blank = True)

    repo_dir      = models.CharField(max_length = 300, verbose_name = 'Clone folder',
                                     help_text = 'The git clone of the repository, with its remote called origin. The same path on every machine that runs a worker. This system never changes files in it.')
    checkout_name = models.CharField(max_length = 80, blank = True,
                                     help_text = 'Folder name of a test checkout. Leave empty to use the name of the clone folder. indianlaw code needs it to be indianlaw.')
    python        = models.CharField(max_length = 300, default = 'python3', verbose_name = 'Python',
                                     help_text = 'The interpreter of this repository\'s own environment, for example $HOME/.egazette/bin/python.')
    env_text      = models.TextField(blank = True, verbose_name = 'Environment variables',
                                     help_text = 'One NAME=value per line, given to the tests. Visible to administrators and stored in the system\'s settings file: do not put production secrets here.')

    schedule      = models.BooleanField(default = True, verbose_name = 'Test automatically', help_text = 'Run the scheduled branches by themselves when they have new commits.')
    cron_hour     = models.CharField(max_length = 40, default = '21', verbose_name = 'At hour', help_text = 'Cron syntax: 21, or 6,18, or */6.')
    cron_minute   = models.CharField(max_length = 40, default = '0', verbose_name = 'At minute', help_text = 'Cron syntax: 0, or 0,30.')

    worker_count  = models.PositiveSmallIntegerField(default = 3, verbose_name = 'Number of workers',
                                                     help_text = 'Each module of a run goes to its own worker; this many modules run at the same time.')
    worker_names  = models.CharField(max_length = 300, blank = True, verbose_name = 'Worker names',
                                     help_text = 'Leave empty for KEY-w1, KEY-w2, ... Or list existing workers, separated by commas.')
    max_runs      = models.PositiveIntegerField(default = 8, help_text = 'Runs of this repository in progress at once; an extra run waits.')
    max_shards    = models.PositiveIntegerField(default = 32, help_text = 'Most module builds of one run; with more modules they are grouped.')
    max_parallel  = models.PositiveIntegerField(default = 0, help_text = 'Most module builds at the same time. 0 means one per worker.')
    step_timeout  = models.PositiveIntegerField(default = 7200, verbose_name = 'Silence limit (seconds)', help_text = 'A module build that prints nothing for this long is stopped.')
    step_max_time = models.PositiveIntegerField(default = 21600, verbose_name = 'Time limit (seconds)', help_text = 'A module build is stopped after this long.')

    position      = models.PositiveIntegerField(default = 0)
    created       = models.DateTimeField(auto_now_add = True)
    updated       = models.DateTimeField(auto_now = True)

    class Meta:
        db_table = 'ci_repos'
        ordering = ['position', 'name']

    def __str__(self):
        return self.name or self.key

    @property
    def adapter_class(self):
        return ADAPTERS.get(hub_repos.ADAPTER_ALIASES.get(self.adapter, self.adapter))

    @property
    def adapter_label(self):
        return self.adapter_class.label if self.adapter_class else self.adapter

    def env(self):
        return hub_repos.parse_env_lines(self.env_text)

    def worker_list(self):
        names = [n.strip() for n in self.worker_names.replace('\n', ',').split(',') if n.strip()]
        return names or ['%s-w%d' % (self.key, i) for i in range(1, self.worker_count + 1)]

    def branch_list(self):
        return list(self.branches.all()) if self.pk else []

    @property
    def branch_names(self):
        return [b.name for b in self.branch_list()]

    def to_config(self, branches = None):
        """The repo profile as the master and the runner read it."""
        branches = self.branch_list() if branches is None else branches
        try:
            env = self.env()
        except ValueError:
            env = {}
        return hub_repos.RepoConfig(
            key = self.key, name = self.name, description = self.description, enabled = self.enabled,
            adapter = self.adapter, options = dict(self.options or {}), repo_dir = self.repo_dir,
            checkout_name = self.checkout_name, python = self.python, env = env,
            branches = [hub_repos.BranchConfig(b.name, b.scheduled, b.cron_hour.strip(), b.cron_minute.strip()) for b in branches],
            schedule = self.schedule, cron_hour = self.cron_hour, cron_minute = self.cron_minute,
            workers = self.worker_list(), max_runs = self.max_runs, max_shards = self.max_shards,
            max_parallel = self.max_parallel, step_timeout = self.step_timeout, step_max_time = self.step_max_time)


class Branch(models.Model):
    """A branch of a repo profile that people test."""
    repo      = models.ForeignKey(RepoProfile, on_delete = models.CASCADE, related_name = 'branches')
    name      = models.CharField(max_length = 100, validators = [BRANCH_VALIDATOR])
    scheduled = models.BooleanField(default = True, verbose_name = 'Test automatically',
                                    help_text = 'Tested by itself when it has new commits (needs "Test automatically" on the repository).')
    cron_hour   = models.CharField(max_length = 40, blank = True, default = '', verbose_name = 'Hour',
                                   help_text = "Hour of its nightly run, such as 21 or 3,15. Empty: the repository's own time.")
    cron_minute = models.CharField(max_length = 40, blank = True, default = '', verbose_name = 'Minute',
                                   help_text = "Minute of its nightly run, such as 0 or */20. Empty: the repository's own time.")
    position  = models.PositiveIntegerField(default = 0)

    class Meta:
        db_table = 'ci_repo_branches'
        ordering = ['position', 'id']
        constraints = [models.UniqueConstraint(fields = ['repo', 'name'], name = 'ci_repo_branch_unique')]

    def __str__(self):
        return '%s:%s' % (self.repo.key, self.name)
