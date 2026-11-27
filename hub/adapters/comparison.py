"""The indianlaw regression suite: every test writes an output file that is compared with a committed expected file.

The contract with the repository (its `regressions/regression.py`) is described in CLAUDE.md. The tests run inside the
runner process, in the Python environment of the repo profile, so this adapter imports the application code itself.
"""
import inspect
import os
import sys
import traceback

from hub import diffing, diffparse, dotenv_merge, selection
from hub.adapters.base import Adapter, Field, Outcome, PATH, TEXT, Session
from hub.store import FAILED, PASSED

CI_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class OutputComparisonSession(Session):
    def __init__(self, regression, storage, srcs, error = None):
        self.regression = regression
        self.storage    = storage
        self.srcs       = srcs
        self.error      = error
        self.objects    = {}
        self.failed     = {}

    def get_obj(self, module):
        if self.error:
            raise self.error
        if module in self.failed:
            raise self.failed[module]
        if module not in self.objects:
            try:
                self.objects[module] = self.regression.load_regress_objs(self.storage, self.srcs, [module])[module]
            except Exception as exc:
                self.failed[module] = exc
                raise
        return self.objects[module]


class OutputComparisonAdapter(Adapter):
    kind        = 'comparison'
    label       = 'Output comparison'
    best_for    = 'Repositories that use the Indian Kanoon regression framework.'
    summary     = ('Each test writes an output that is compared with a stored expected file, and any difference is shown line by line. '
                   'For repositories that use the Indian Kanoon regression framework (tests listed by regressions/regression.py, '
                   'expected files in regressions/expected/).')
    result_kind = 'diff'
    fields = (
        Field('env_profile', 'Environment profile', TEXT, 'local', required = True,
              help = 'The application profile the tests run with (the -P option of regression.py), for example local.'),
        Field('profile_file', 'Machine settings file', PATH, '',
              help = 'Optional, on each worker: a private file with only the settings that differ on that machine, for example '
                     'DATADIR=/home/you/indianlaw_data. It is merged into .env.<profile> of each checkout; every other setting is kept.',
              placeholder = '$HOME/.buildbotci/private/indianlaw.env'),
        Field('loglevel', 'Log level', TEXT, 'info', help = 'Level of the application logging inside the tests.'),
    )

    @classmethod
    def has_prepare(cls, options):
        return bool((options or {}).get('profile_file'))

    # ---- the checkout ---------------------------------------------------------------------------------------

    def activate(self):
        regressions = os.path.join(self.tree, 'regressions')
        wanted = [CI_DIR, regressions, os.path.join(self.tree, 'website'), self.tree, os.path.dirname(self.tree)]
        sys.path[:] = wanted + [p for p in sys.path if p not in wanted]
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pylaw.settings')
        os.environ['OMP_NUM_THREADS'] = '1'
        os.chdir(regressions)

    def prepare_checkout(self):
        source = os.path.expanduser(os.path.expandvars(self.options['profile_file']))
        if not os.path.isfile(source):
            raise SystemExit('Settings file not found: %s' % source)
        target = os.path.join(self.tree, '.env.%s' % self.options['env_profile'])
        existing = ''
        if os.path.exists(target):
            with open(target, 'r', encoding = 'utf8') as handle:
                existing = handle.read()
        with open(source, 'r', encoding = 'utf8') as handle:
            merged, applied = dotenv_merge.merge(existing, handle.read())
        with open(target, 'w', encoding = 'utf8') as handle:
            handle.write(merged)
        return 'settings: %d from %s applied to .env.%s%s' % (
            len(applied), source, self.options['env_profile'], (': ' + ', '.join(applied)) if applied else ' (nothing needed changing)')

    def import_regression(self):
        import regression
        return regression

    # ---- catalog ---------------------------------------------------------------------------------------------

    def catalog(self):
        regression = self.import_regression()
        if not hasattr(regression, 'MODULES'):
            raise RuntimeError('This branch has no regression.MODULES in regressions/regression.py, so its tests cannot be listed. '
                               'The regression.py change has not been merged into it yet.')
        tests, modules = [], {}
        for name, (cls, _) in regression.MODULES.items():
            modules[name] = {'class': cls.__name__, 'source': os.path.relpath(inspect.getsourcefile(cls), self.tree)}
            for tname in regression.get_class_tests(cls):
                func   = getattr(cls, tname)
                takes  = 'outhandle' in inspect.signature(func).parameters
                output = regression.output_filename(tname) if takes else None
                doc    = (inspect.getdoc(func) or '').split('\n')[0]
                tests.append({'id': '%s.%s' % (name, tname), 'module': name, 'name': tname, 'output': output, 'doc': doc,
                              'has_expected': bool(output) and os.path.exists(os.path.join('expected', output))})
        return {'modules': modules, 'tests': tests}

    # ---- running ---------------------------------------------------------------------------------------------

    def load_environment(self):
        regression = self.import_regression()
        from indianlaw.setup.data_srcs import DataSrcFactory
        from indianlaw.utils import commonfuncs as utilscommon
        from indianlaw.storage_manager import StorageManager

        utilscommon.setup_logging(self.options['loglevel'])
        storage = StorageManager(self.options['env_profile'])
        srcs    = DataSrcFactory(storage.get_datadir()).get_all_srcs([])
        return regression, storage, srcs

    def open(self):
        try:
            regression, storage, srcs = self.load_environment()
            return OutputComparisonSession(regression, storage, srcs)
        except Exception as exc:
            traceback.print_exc()
            return OutputComparisonSession(self.import_regression(), None, [], error = exc)

    def impact_map_path(self):
        return os.path.join(CI_DIR, 'hub', 'impact_map.json')

    def diff_rules(self):
        impact = selection.load_impact_map(self.impact_map_path())
        return diffparse.compile_markers(impact.get('context_markers', [])), diffparse.compile_formats(impact.get('record_formats', []))

    def run_test(self, session, entry, scratch):
        regression = session.regression
        obj = session.get_obj(entry['module'])
        if '__initialize__' in dir(obj):
            obj.__initialize__()
        method = getattr(obj, entry['name'])

        if not entry['output']:
            retval = method()
            if retval:
                return Outcome(PASSED)
            return Outcome(FAILED, 'Test returned %r (falsy) instead of a truthy value' % (retval,))

        actual   = os.path.join(scratch, 'actual')
        expected = os.path.join('expected', entry['output'])
        labels   = ('expected/%s' % entry['output'], 'actual/%s' % entry['output'])
        with open(actual, 'w', encoding = 'utf8', newline = '') as outhandle:
            method(outhandle)
        status, difftext = regression.compare_files(expected, actual, labels = labels)
        outcome = Outcome(status, actual = actual, expected = expected)
        if status == regression.FAILED:
            markers, formats = self.diff_rules()
            git_diff = diffing.histogram_diff(expected, actual, labels, scratch)
            if git_diff is None:
                print('note: git diff --histogram could not be used; diff.txt is the plain diff -u')
            outcome.diff, outcome.stats, outcome.hunks = diffing.analyze_failure(
                git_diff or difftext, expected, markers, diffparse.rule_for(entry['output'], formats))
            outcome.reason = diffing.describe_diff(outcome.stats, outcome.hunks)
        elif status == regression.NO_EXPECTED:
            outcome.reason = 'No expected file at regressions/expected/%s; the test produced %s bytes of output' % (
                entry['output'], diffing.file_size(actual))
        return outcome

    def reproduce(self, entry):
        env = self.options['env_profile']
        return 'cd regressions\npython regression.py -P %s -m %s -t %s' % (env, entry['module'], entry['name'])

    def baseline_note(self, entry):
        env = self.options['env_profile']
        return ('cd regressions\npython regression.py -P %s -m %s -t %s -p' % (env, entry['module'], entry['name']),
                'Writes regressions/expected/%s from the current output. Review it before committing.' % entry.get('output'))
