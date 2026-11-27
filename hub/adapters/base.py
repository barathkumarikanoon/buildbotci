"""What every kind of test suite has to provide.

An *adapter* is the part of the CI that knows one way of testing: how to list the tests of a checkout, and how to run
one of them and judge its result. The rest of the CI (git, Buildbot, workers, shards, the run directory, the web pages)
does not know which adapter it is dealing with.

To support a new kind of suite, subclass `Adapter`, give it a `kind`, a `label` and its `fields` (the web UI builds the
form from them), and add it to `hub/adapters/__init__.py`.
"""
import os
from dataclasses import dataclass, field

TEXT, PATH, LINES, BOOL, INT, CHOICE = 'text', 'path', 'lines', 'bool', 'int', 'choice'


@dataclass
class Field:
    """One setting of an adapter, as the repo profile form shows it."""
    name:        str
    label:       str
    kind:        str = TEXT
    default:     object = ''
    help:        str = ''
    required:    bool = False
    choices:     tuple = ()          # for CHOICE: ((value, label), ...)
    placeholder: str = ''


@dataclass
class Outcome:
    """What running one test came to. The executor stores it in the run directory."""
    status:   str
    reason:   str = None
    actual:   str = None             # path of the file the test produced (kept when the test did not pass)
    expected: str = None             # path of the file it was compared with (kept when the test did not pass)
    diff:     str = None             # unified diff text (written to diff.txt)
    stats:    dict = None            # summary of the diff
    hunks:    list = field(default_factory = list)
    extra:    dict = field(default_factory = dict)    # adapter-specific facts, merged into result.json


class Session:
    """What an adapter loads once per process and reuses for every test. `error` is set when loading failed:
    every test of the process then reports it, instead of the process dying."""
    error = None


class Adapter:
    kind    = ''
    label   = ''
    summary = ''
    best_for = ''          # one line for the card in the Add a repository form: who this type is for
    fields  = ()
    result_kind = 'output'           # 'diff': tests are judged by comparing output with an expected file

    def __init__(self, repo, tree, options):
        self.repo    = repo          # RepoConfig
        self.tree    = tree          # the checkout the tests are read from
        self.options = self.with_defaults(options)

    # ---- settings -------------------------------------------------------------------------------------------

    @classmethod
    def with_defaults(cls, options):
        values = {f.name: f.default for f in cls.fields}
        values.update({k: v for k, v in (options or {}).items() if k in values})
        return values

    @classmethod
    def clean_options(cls, raw):
        """Check the settings typed in the form. Returns (options, {field name: problem})."""
        options, problems = {}, {}
        for f in cls.fields:
            value = raw.get(f.name, f.default)
            if f.kind == BOOL:
                options[f.name] = bool(value) and value not in ('0', 'false', 'False', 'off')
                continue
            text = ('' if value is None else str(value)).strip() if f.kind != LINES else ('' if value is None else str(value)).strip('\n')
            if not text:
                if f.required:
                    problems[f.name] = 'This setting is required.'
                options[f.name] = f.default
                continue
            if f.kind == INT:
                try:
                    options[f.name] = int(text)
                except ValueError:
                    problems[f.name] = 'Enter a whole number.'
                    continue
                if options[f.name] < 0:
                    problems[f.name] = 'Must be 0 or more.'
            elif f.kind == CHOICE:
                if text not in [c[0] for c in f.choices]:
                    problems[f.name] = 'Choose one of the listed values.'
                options[f.name] = text
            else:
                options[f.name] = text
        problems.update(cls.check_options(options, problems))
        return options, problems

    @classmethod
    def check_options(cls, options, problems):
        """Checks that need more than one setting. Returns {field name: problem}."""
        return {}

    @classmethod
    def has_prepare(cls, options):
        """True when the module builds need a `prepare` step after the checkout."""
        return False

    # ---- running (these run in the worker's runner process) --------------------------------------------------

    def activate(self):
        """Make the checkout usable from this process (import paths, working folder, environment)."""

    def prepare_checkout(self):
        """The step after a checkout (only run when `has_prepare`). Returns a line to print."""
        return ''

    def catalog(self):
        """{'modules': {name: {...}}, 'tests': [{'id', 'module', 'name', 'doc', 'output', 'has_expected'}]}.
        `id` is `<module>.<name>`, and must be usable as a folder name."""
        raise NotImplementedError

    def open(self):
        """Load what the tests need, once. Returns a `Session`."""
        return Session()

    def run_test(self, session, entry, scratch):
        """Run one test of the catalog; `scratch` is a folder to write temporary files into. Returns an `Outcome`.
        Anything printed goes to the test's log. An exception is recorded as the status `error`."""
        raise NotImplementedError

    def reproduce(self, entry):
        """How a developer runs this test by hand, shown on the test page. None if there is no such way."""
        return None

    def baseline_note(self, entry):
        """Shown when the test has no expected output yet (only for adapters that compare outputs)."""
        return None

    def impact_map_path(self):
        """A JSON file mapping changed files to tests, or None (every run of the repo is then complete)."""
        return None

    def diff_rules(self):
        """(context markers, record formats) that make diffs of this suite readable, both compiled."""
        return [], ()

    # ---- helpers for subclasses ------------------------------------------------------------------------------

    def process_env(self):
        """The environment of a process that runs this repo's code: the repo's own Python first on PATH."""
        env = dict(os.environ)
        env.update(self.repo.env)
        python = self.repo.python
        if os.path.isabs(python):
            env['PATH'] = os.path.dirname(python) + os.pathsep + env.get('PATH', os.defpath)
            bin_dir = os.path.dirname(python)
            if os.path.exists(os.path.join(os.path.dirname(bin_dir), 'pyvenv.cfg')):
                env['VIRTUAL_ENV'] = os.path.dirname(bin_dir)
        env['PYTHONUNBUFFERED'] = '1'
        env['CI_REPO'] = self.repo.key
        return env
