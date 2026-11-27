"""A pytest suite: every collected test is a test of the CI, grouped by the file it is in."""
import hashlib
import os
import re
import subprocess
import xml.etree.ElementTree as ET

from hub.adapters.base import Adapter, Field, Outcome, INT, PATH, TEXT
from hub.adapters.process import run_streaming
from hub.store import ERROR, FAILED, PASSED

UNSAFE = re.compile(r'[^A-Za-z0-9._-]')


def test_id(nodeid, taken):
    """A folder-safe id for a pytest node id: tests/test_chat.py::TestBot::test_a[x] -> tests.test_chat.TestBot.test_a_x_"""
    path, _, rest = nodeid.partition('::')
    module = re.sub(r'\.py$', '', path).replace('/', '.')
    name   = UNSAFE.sub('_', rest.replace('::', '.')) or 'test'
    ident  = UNSAFE.sub('_', '%s.%s' % (module, name)).lstrip('._-') or 'test'
    if len(ident) > 120 or ident in taken:
        ident = '%s_%s' % (ident[:110], hashlib.sha1(nodeid.encode('utf8')).hexdigest()[:6])
    taken.add(ident)
    return ident, UNSAFE.sub('_', module).lstrip('._-') or 'tests', name


class PytestAdapter(Adapter):
    kind    = 'pytest'
    label   = 'pytest (Python tests)'
    best_for = 'Python projects whose tests run with pytest.'
    summary = 'Collects tests with pytest and runs each one on its own. A test passes when pytest says so.'
    fields = (
        Field('paths', 'Test paths', TEXT, 'tests', required = True, help = 'Files or folders pytest collects from, separated by spaces.'),
        Field('args', 'Extra pytest options', TEXT, '', help = 'For example  -m "not slow"  or  -k chatbot.'),
        Field('cwd', 'Run in folder', PATH, '.', help = 'Folder inside the checkout pytest runs in.'),
        Field('timeout', 'Time limit per test (seconds)', INT, 1800),
    )

    @classmethod
    def check_options(cls, options, problems):
        value = options.get('cwd') or ''
        if os.path.isabs(value) or '..' in value.split('/'):
            return {'cwd': 'Use a folder inside the checkout (no leading / and no ..).'}
        return {}

    def base_command(self):
        return [self.repo.python, '-m', 'pytest', '-p', 'no:cacheprovider'] + self.options['args'].split()

    def cwd(self):
        return os.path.join(self.tree, self.options['cwd'])

    def catalog(self):
        command = self.base_command() + ['--collect-only', '-q'] + self.options['paths'].split()
        try:
            done = subprocess.run(command, cwd = self.cwd(), env = self.process_env(), stdout = subprocess.PIPE,
                                  stderr = subprocess.STDOUT, timeout = 900)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError('Could not run pytest with %s: %s' % (self.repo.python, exc))
        text = done.stdout.decode('utf8', 'replace')
        nodes = [line.strip() for line in text.splitlines() if '::' in line and not line.startswith(' ')]
        if done.returncode not in (0, 5) or (done.returncode == 5 and not nodes):
            tail = '\n'.join(text.strip().splitlines()[-8:])
            raise RuntimeError('pytest could not list the tests (exit %s):\n%s' % (done.returncode, tail))
        modules, tests, taken = {}, [], set()
        for node in nodes:
            ident, module, name = test_id(node, taken)
            modules.setdefault(module, {'class': '', 'source': node.partition('::')[0]})
            tests.append({'id': ident, 'module': module, 'name': name, 'doc': node, 'output': None,
                          'has_expected': False, 'nodeid': node})
        return {'modules': modules, 'tests': tests}

    def run_test(self, session, entry, scratch):
        report = os.path.join(scratch, 'report.xml')
        output = os.path.join(scratch, 'actual')
        command = self.base_command() + ['-q', '-rA', '--junitxml', report, entry['nodeid']]
        print('$ %s' % ' '.join(command[2:]))
        code, timed_out = run_streaming(command, self.cwd(), self.process_env(), output, self.options['timeout'])
        if timed_out:
            return Outcome(ERROR, 'Timed out after %s seconds' % self.options['timeout'], actual = output)
        reason = self.first_problem(report)
        if code == 0:
            return Outcome(PASSED, 'Skipped by pytest' if reason == 'skipped' else None)
        if code == 1:
            return Outcome(FAILED, reason or 'pytest reported a failure', actual = output)
        if code == 5:
            return Outcome(ERROR, 'pytest collected no test for %s' % entry['nodeid'], actual = output)
        return Outcome(ERROR, reason or 'pytest exited with code %s' % code, actual = output)

    @staticmethod
    def first_problem(report):
        try:
            root = ET.parse(report).getroot()
        except (OSError, ET.ParseError):
            return None
        for case in root.iter('testcase'):
            for tag in ('failure', 'error'):
                found = case.find(tag)
                if found is not None:
                    return ((found.get('message') or found.text or tag).strip().splitlines() or [tag])[0][:300]
            if case.find('skipped') is not None:
                return 'skipped'
        return None

    def reproduce(self, entry):
        return 'cd %s\n%s' % (self.options['cwd'], ' '.join(['python', '-m', 'pytest'] + self.options['args'].split() + [entry.get('nodeid', entry['id'])]))
