import re

LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$')


def parse(text):
    settings = {}
    for raw in text.splitlines():
        if raw.lstrip().startswith('#'):
            continue
        match = LINE.match(raw)
        if match:
            settings[match.group(1)] = match.group(2).strip()
    return settings


def merge(existing, overrides):
    wanted  = parse(overrides)
    found   = set()
    applied = []
    lines   = []
    for raw in existing.splitlines():
        match = None if raw.lstrip().startswith('#') else LINE.match(raw)
        if match and match.group(1) in wanted:
            name = match.group(1)
            lines.append('%s=%s' % (name, wanted[name]))
            if name not in found and match.group(2).strip() != wanted[name]:
                applied.append(name)
            found.add(name)
        else:
            lines.append(raw)
    for name, value in wanted.items():
        if name not in found:
            lines.append('%s=%s' % (name, value))
            applied.append(name)
    return '\n'.join(lines) + '\n', applied
