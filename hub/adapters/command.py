"""Any suite you can run from a shell: each check is a command, judged by its exit code, or by its output compared
with a golden file (so a command can be a diff-based test too)."""
import os

from hub import diffing
from hub.adapters.base import Adapter, Field, Outcome, CHOICE, INT, LINES, PATH
from hub.adapters.process import run_streaming
from hub.store import ERROR, FAILED, ID_RE, NO_EXPECTED, PASSED

DEFAULT_MODULE = 'checks'


def parse_checks(text):
    """`name | command` lines to [(name, command)]. A name such as `smoke.import` puts the check in the module `smoke`;
    without a dot it goes in the module `checks`. Raises ValueError with the line number of a bad line."""
    checks, seen = [], set()
    for number, raw in enumerate((text or '').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        name, sep, command = line.partition('|')
        name, command = name.strip(), command.strip()
        if not sep or not name or not command:
            raise ValueError('line %d: write it as  name | command' % number)
        if not ID_RE.match(name) or '..' in name:
            raise ValueError('line %d: %r is not a usable name (letters, digits, dot, dash, underscore)' % (number, name))
        if name in seen:
            raise ValueError('line %d: the name %s is used twice' % (number, name))
        seen.add(name)
        checks.append((name, command))
    return checks


class CommandAdapter(Adapter):
    kind        = 'command'
    label       = 'Command checks'
    best_for    = 'Anything else: list the shell commands to run.'
    summary     = 'Each check is a shell command run in the repository. It passes when it exits with the expected code, or, when you choose to compare outputs, when its output also equals a saved file.'
    fields = (
        Field('checks', 'Checks', LINES, '', required = True, placeholder = 'smoke.imports | python -c "import sync"\nlint.compile | python -m compileall -q .',
              help = 'One check per line:  name | command. A name like smoke.imports puts the check in the module "smoke" '
                     '(the unit of parallel work: each module runs on its own worker). Commands run in the repository checkout '
                     'with the repository profile\'s Python first on PATH.'),
        Field('cwd', 'Run in folder', PATH, '.', help = 'Folder inside the checkout the commands run in.'),
        Field('compare', 'A check passes when', CHOICE, 'exit',
              choices = (('exit', 'the command exits with the expected code'),
                         ('golden', 'it exits as expected and its output equals the saved file')),
              help = 'Comparing outputs checks the output against <folder of saved outputs>/<name>.txt and shows any difference line by line, as the Output comparison type does.'),
        Field('golden_dir', 'Folder of saved outputs', PATH, 'expected', help = 'Inside the checkout. Only used when outputs are compared.'),
        Field('expected_exit', 'Expected exit code', INT, 0),
        Field('timeout', 'Time limit per check (seconds)', INT, 1800),
    )

    @classmethod
    def check_options(cls, options, problems):
        found = {}
        if options.get('checks') and 'checks' not in problems:
            try:
                parse_checks(options['checks'])
            except ValueError as exc:
                found['checks'] = str(exc)
        for name in ('cwd', 'golden_dir'):
            value = options.get(name) or ''
            if os.path.isabs(value) or '..' in value.split('/'):
                found[name] = 'Use a folder inside the checkout (no leading / and no ..).'
        if int(options.get('timeout') or 0) < 1 and 'timeout' not in problems:
            found['timeout'] = 'Must be at least 1.'
        return found

    def golden_path(self, entry):
        return os.path.join(self.tree, self.options['golden_dir'], '%s.txt' % entry['id'])

    def catalog(self):
        modules, tests = {}, []
        for name, command in parse_checks(self.options['checks']):
            module, _, short = name.partition('.')
            if not short:
                module, short = DEFAULT_MODULE, name
            modules.setdefault(module, {'class': '', 'source': ''})
            golden = self.options['compare'] == 'golden'
            tests.append({'id': name, 'module': module, 'name': short, 'doc': command,
                          'output': ('%s.txt' % name) if golden else None,
                          'has_expected': golden and os.path.exists(self.golden_path({'id': name}))})
        if not tests:
            raise RuntimeError('No checks are configured for this repository profile. Add them in the website.')
        return {'modules': modules, 'tests': tests}

    def command_of(self, entry):
        for name, command in parse_checks(self.options['checks']):
            if name == entry['id']:
                return command
        raise RuntimeError('The check %s is no longer configured' % entry['id'])

    def run_test(self, session, entry, scratch):
        command = self.command_of(entry)
        actual  = os.path.join(scratch, 'actual')
        cwd     = os.path.join(self.tree, self.options['cwd'])
        print('$ %s' % command)
        code, timed_out = run_streaming(command, cwd, self.process_env(), actual, self.options['timeout'], shell = True)
        if timed_out:
            return Outcome(ERROR, 'Timed out after %s seconds' % self.options['timeout'], actual = actual)
        wanted = self.options['expected_exit']
        if code != wanted:
            return Outcome(FAILED, 'Exited with code %s instead of %s' % (code, wanted), actual = actual)
        if self.options['compare'] != 'golden':
            return Outcome(PASSED)
        return self.compare_golden(entry, actual, scratch)

    def compare_golden(self, entry, actual, scratch):
        expected = self.golden_path(entry)
        if not os.path.exists(expected):
            return Outcome(NO_EXPECTED, 'No golden file at %s; the check printed %s bytes' % (
                os.path.relpath(expected, self.tree), diffing.file_size(actual)), actual = actual)
        with open(expected, 'rb') as one, open(actual, 'rb') as two:
            if one.read() == two.read():
                return Outcome(PASSED, expected = expected)
        labels = ('expected/%s' % entry['id'], 'actual/%s' % entry['id'])
        text   = diffing.histogram_diff(expected, actual, labels, scratch)
        outcome = Outcome(FAILED, actual = actual, expected = expected)
        if text is None:
            outcome.reason = 'Output differs from the golden file (git could not make the diff)'
            return outcome
        outcome.diff, outcome.stats, outcome.hunks = diffing.analyze_failure(text, expected, [], None)
        outcome.reason = diffing.describe_diff(outcome.stats, outcome.hunks)
        return outcome

    def reproduce(self, entry):
        try:
            return 'cd %s\n%s' % (self.options['cwd'], self.command_of(entry))
        except RuntimeError:
            return None

    def baseline_note(self, entry):
        if self.options['compare'] != 'golden':
            return None
        folder = self.options['golden_dir']
        return ('cd %s\n%s > %s/%s.txt' % (self.options['cwd'], self.command_of(entry), folder, entry['id']),
                'Saves the current output as the golden file. Review it before committing.')
