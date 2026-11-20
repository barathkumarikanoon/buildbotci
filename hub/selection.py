import fnmatch
import json
import os
import re
import subprocess

SUFFIX_RE = re.compile(r'_(docs|lawyer_test)$')


def load_impact_map(path):
    with open(path, 'r', encoding = 'utf8') as handle:
        return json.load(handle)


def _git(repo, *args):
    return subprocess.run(['git', '-C', repo] + list(args),
                          stdout = subprocess.PIPE, stderr = subprocess.PIPE)


def head_revision(repo):
    p = _git(repo, 'rev-parse', 'HEAD')
    return p.stdout.decode().strip() if p.returncode == 0 else None


def resolve_base(repo, last_tested, head = 'HEAD'):
    if last_tested and _git(repo, 'cat-file', '-e', last_tested + '^{commit}').returncode == 0:
        p = _git(repo, 'merge-base', last_tested, head)
        if p.returncode == 0:
            return p.stdout.decode().strip()
    p = _git(repo, 'rev-parse', '--verify', '-q', head + '~1')
    if p.returncode == 0:
        return p.stdout.decode().strip()
    return None


def changed_files(repo, base, head = 'HEAD'):
    p = _git(repo, 'diff', '--name-only', '--no-renames', base, head)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode('utf8', 'replace'))
    return [line for line in p.stdout.decode('utf8').splitlines() if line]


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def expand_specs(specs, catalog):
    tests   = catalog['tests']
    modules = catalog['modules']
    chosen  = set()
    unmatched = []

    for spec in specs:
        found = set()
        if spec in ('all', '*'):
            found = {t['id'] for t in tests}
        elif spec in modules:
            found = {t['id'] for t in tests if t['module'] == spec}
        else:
            for t in tests:
                if spec == t['id'] or spec == t['name'] or fnmatch.fnmatchcase(t['id'], spec):
                    found.add(t['id'])
        if not found:
            unmatched.append(spec)
        chosen |= found

    ordered = [t['id'] for t in tests if t['id'] in chosen]
    return ordered, unmatched


def split_specs(text):
    return [part for part in re.split(r'[\s,]+', text or '') if part]


def _names(path, aliases):
    stem = os.path.splitext(os.path.basename(path))[0]
    name = SUFFIX_RE.sub('', stem)
    names = [name]
    for key, values in aliases.items():
        if fnmatch.fnmatchcase(name, key):
            names.extend(v for v in values if v not in names)
    return names


def impacted(files, impact_map, catalog):
    tests    = catalog['tests']
    modules  = catalog['modules']
    ids      = {t['id'] for t in tests}
    reasons  = {}
    unmapped = []
    ignored  = []
    full_by  = []

    def add(found, reason):
        for test_id in found:
            reasons.setdefault(test_id, [])
            if reason not in reasons[test_id]:
                reasons[test_id].append(reason)

    def by_module(names):
        return {t['id'] for t in tests if t['module'] in names}

    for path in files:
        if matches(path, impact_map.get('ignore', [])):
            ignored.append(path)
            continue

        if matches(path, impact_map.get('full', [])):
            full_by.append(path)
            continue

        hit = False

        if path.startswith('regressions/expected/'):
            output = path[len('regressions/expected/'):]
            found  = {t['id'] for t in tests if t.get('output') == output}
            if found:
                add(found, 'expected file changed: %s' % path)
                hit = True

        owners = [name for name, info in modules.items() if info.get('source') == path]
        if owners:
            add(by_module(owners), 'regression module changed: %s' % path)
            hit = True

        for rule in impact_map.get('rules', []):
            if not matches(path, rule['paths']):
                continue
            hit = True
            label = '%s (%s)' % (rule.get('name', 'rule'), path)
            found = by_module(rule.get('modules', []))
            if 'tests' in rule:
                templated = set()
                for template in rule['tests']:
                    for name in _names(path, rule.get('aliases', {})):
                        pattern = template.format(name = name)
                        templated |= {i for i in ids if fnmatch.fnmatchcase(i, pattern)}
                if not templated and rule.get('fallback_modules'):
                    templated = by_module(rule['fallback_modules'])
                found |= templated
            add(found, label)

        if not hit:
            unmapped.append(path)

    full = bool(full_by)
    if unmapped and impact_map.get('unmapped', 'full') == 'full':
        full = True
        full_by.extend(unmapped)

    if full:
        selected = [t['id'] for t in tests]
        for test_id in selected:
            reasons.setdefault(test_id, [])
        add(selected, 'full run: %s' % ', '.join(full_by[:5]))
    else:
        selected = [t['id'] for t in tests if t['id'] in reasons]

    return {'tests': selected, 'reasons': reasons, 'full': full,
            'unmapped': unmapped, 'ignored': ignored, 'full_by': full_by}
