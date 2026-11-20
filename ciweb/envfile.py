import os
import re

CONFLICTS = []
LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*?)\s*$")


def parse(text):
    values = {}
    for raw in text.splitlines():
        if raw.lstrip().startswith('#'):
            continue
        match = LINE.match(raw)
        if not match:
            continue
        name, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] == "'":
            value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] and value[0] == '"':
            value = os.path.expandvars(value[1:-1])
        else:
            value = os.path.expanduser(os.path.expandvars(value))
        values[name] = value
    return values


def load(path = None):
    del CONFLICTS[:]
    path = path or os.environ.get('REG_ENV_FILE') or os.path.expanduser('~/bb-master.env')
    try:
        with open(path, 'r', encoding = 'utf8') as handle:
            values = parse(handle.read())
    except OSError:
        return {}
    for name, value in values.items():
        current = os.environ.get(name)
        if current is None:
            os.environ[name] = value
        elif current != value:
            CONFLICTS.append(name)
    return values
