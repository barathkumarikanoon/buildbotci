"""A read-only look at a clone folder, to fill in most of the "Add a repository" form for the administrator.

Nothing here runs a program of the repository, and nothing is changed: it asks git a few questions about the folder, looks
for the files that say how its tests are run, and checks that the interpreter exists. The answers are advice, never a reason to
refuse a save.
"""
import os
import re
import shutil
import subprocess

from hub import repos as hub_repos

PYTEST_FILES = ('pytest.ini', 'conftest.py')


def git(path, *args):
    done = subprocess.run(['git', '-C', path] + list(args), stdout = subprocess.PIPE, stderr = subprocess.PIPE, timeout = 10)
    return done.returncode, done.stdout.decode('utf8', 'replace').strip()


def mask(url):
    """The address of origin without a password or token in it."""
    return re.sub(r'://[^/@\s]+@', '://', url)


def slug(text):
    """A key the profile accepts: a letter, then letters, digits and hyphens (hub/repos.py: KEY_RE), at most 32 characters."""
    text = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    text = re.sub(r'^[^a-z]+', '', text)
    return text[:32].rstrip('-')


def pretty(text):
    return ' '.join(part.capitalize() for part in re.split(r'[-_\s]+', text) if part)


def read(path):
    try:
        with open(path, encoding = 'utf8', errors = 'replace') as handle:
            return handle.read()
    except OSError:
        return ''


def detect_suite(path):
    """(kind, why): how the tests of the folder are probably run."""
    if os.path.isfile(os.path.join(path, 'regressions', 'regression.py')):
        return 'comparison', 'Found regressions/regression.py: the Indian Kanoon regression framework.'
    for name in PYTEST_FILES:
        if os.path.isfile(os.path.join(path, name)):
            return 'pytest', 'Found %s.' % name
    for name, marker in (('pyproject.toml', '[tool.pytest'), ('setup.cfg', '[tool:pytest'), ('tox.ini', '[pytest]')):
        if marker in read(os.path.join(path, name)):
            return 'pytest', 'Found pytest settings in %s.' % name
    tests = os.path.join(path, 'tests')
    if os.path.isdir(tests) and any(n.startswith('test_') or n.endswith('_test.py') for n in os.listdir(tests)):
        return 'pytest', 'Found test files in tests/.'
    return 'command', 'No test framework found: list the checks to run as shell commands.'


def python_candidates(path, key):
    home = os.path.expanduser('~')
    names = [key, os.path.basename(path.rstrip('/'))]
    found = []
    for name in dict.fromkeys(n for n in names if n):
        found += [os.path.join(home, '.' + name, 'bin', 'python')]
    return found + [os.path.join(path, '.venv', 'bin', 'python'), os.path.join(path, 'venv', 'bin', 'python')]


def check_python(text):
    """{'ok', 'text'} for the interpreter field; no program is run."""
    value = hub_repos.expand_command(text or '')
    if not value:
        return {'ok': None, 'text': ''}
    if os.path.isabs(value):
        if os.access(value, os.X_OK) and os.path.isfile(value):
            real = os.path.realpath(value)
            return {'ok': True, 'text': 'Found %s%s.' % (value, '' if real == value else ' (→ %s)' % os.path.basename(real))}
        return {'ok': False, 'text': 'Not found or not executable: %s' % value}
    found = shutil.which(value)
    return {'ok': True, 'text': 'Found on PATH: %s.' % found} if found else {'ok': False, 'text': '%s is not on the PATH of this machine.' % value}


def probe(repo_dir, python = ''):
    answer = {'folder': {'ok': False, 'text': ''}, 'git': {'ok': None, 'text': '', 'origin': ''}, 'branches': [], 'default_branch': '',
              'suggest': {}, 'python': check_python(python)}
    path = hub_repos.expand_path(repo_dir or '')
    if not path:
        answer['folder']['text'] = 'Type the folder of the git clone.'
        return answer
    if not os.path.isdir(path):
        answer['folder']['text'] = 'No folder %s on this machine.' % path
        return answer
    answer['folder'] = {'ok': True, 'text': path}
    try:
        code, top = git(path, 'rev-parse', '--show-toplevel')
        if code != 0:
            answer['git'] = {'ok': False, 'text': 'This folder is not a git repository.', 'origin': ''}
            return answer
        _, url = git(path, 'remote', 'get-url', 'origin')
        if not url:
            answer['git'] = {'ok': False, 'text': 'The clone has no remote called origin; the CI fetches from it.', 'origin': ''}
        else:
            answer['git'] = {'ok': True, 'text': 'Git repository with origin %s' % mask(url), 'origin': mask(url)}
        if os.path.realpath(top) != os.path.realpath(path):
            answer['git']['text'] += '. Note: the repository starts in %s, not here.' % top
        _, listing = git(path, 'for-each-ref', '--format=%(refname:short)', 'refs/remotes/origin')
        names = sorted({n[len('origin/'):] for n in listing.splitlines() if n.startswith('origin/') and n != 'origin/HEAD'})
        answer['branches'] = names
        _, head = git(path, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD')
        default = head[len('origin/'):] if head.startswith('origin/') else ''
        answer['default_branch'] = default if default in names else next((n for n in ('main', 'master') if n in names), names[0] if names else '')
    except (OSError, subprocess.TimeoutExpired):
        answer['git'] = {'ok': False, 'text': 'git could not be run on this folder.', 'origin': ''}
        return answer
    key = slug(os.path.basename(path.rstrip('/')))
    kind, why = detect_suite(path)
    suggested = next((c for c in python_candidates(path, key) if os.access(c, os.X_OK)), '')      # the page decides whether to use it
    home = os.path.expanduser('~')
    if suggested.startswith(home + os.sep):
        suggested = '$HOME' + suggested[len(home):]
    answer['suggest'] = {'key': key, 'name': pretty(key), 'checkout_name': os.path.basename(path.rstrip('/')), 'adapter': kind,
                         'adapter_why': why, 'python': suggested}
    return answer
