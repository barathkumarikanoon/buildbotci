import difflib
import re

HUNK_RE  = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
TOKEN_RE = re.compile(r'\w+|\s+|[^\w\s]', re.UNICODE)

MAX_INTRALINE = 6000
MAX_LABEL     = 160


def parse_unified(text):
    hunks = []
    cur   = None
    old_no = new_no = 0

    for line in text.splitlines():
        m = HUNK_RE.match(line)
        if m:
            old_start = int(m.group(1))
            old_len   = 1 if m.group(2) is None else int(m.group(2))
            new_start = int(m.group(3))
            new_len   = 1 if m.group(4) is None else int(m.group(4))
            cur = {'old_start': old_start, 'old_len': old_len,
                   'new_start': new_start, 'new_len': new_len,
                   'rows': [], 'added': 0, 'removed': 0, 'label': None,
                   'change_line': None}
            hunks.append(cur)
            old_no = old_start if old_len else old_start + 1
            new_no = new_start if new_len else new_start + 1
            continue

        if cur is None or not line:
            if cur is not None and not line:
                cur['rows'].append(('ctx', '', old_no, new_no))
                old_no += 1
                new_no += 1
            continue

        tag, body = line[0], line[1:]
        if tag in '+-' and cur['change_line'] is None:
            cur['change_line'] = old_no if tag == '-' else max(old_no - 1, 1)

        if tag == '-':
            cur['rows'].append(('del', body, old_no, None))
            cur['removed'] += 1
            old_no += 1
        elif tag == '+':
            cur['rows'].append(('add', body, None, new_no))
            cur['added'] += 1
            new_no += 1
        elif tag == ' ':
            cur['rows'].append(('ctx', body, old_no, new_no))
            old_no += 1
            new_no += 1
        elif tag == '\\':
            cur['rows'].append(('note', line, None, None))

    return hunks


def summarize(hunks):
    added   = sum(h['added'] for h in hunks)
    removed = sum(h['removed'] for h in hunks)
    first   = hunks[0]['change_line'] or hunks[0]['old_start'] if hunks else None
    return {'hunks': len(hunks), 'added': added, 'removed': removed,
            'first_line': first}


def compile_markers(patterns):
    return [re.compile(p) for p in patterns]


def label_hunks(hunks, expected_path, markers):
    if not hunks or not markers:
        return hunks

    def anchor(i):
        return hunks[i]['change_line'] or hunks[i]['old_start']

    targets = sorted(range(len(hunks)), key = anchor)
    ti      = 0
    last    = None

    with open(expected_path, 'r', encoding = 'utf8', errors = 'replace') as handle:
        for lineno, line in enumerate(handle, 1):
            line = line.rstrip('\n')
            for marker in markers:
                m = marker.match(line)
                if m:
                    last = (m.group(1) if m.groups() else line)[:MAX_LABEL]
                    break
            while ti < len(targets) and anchor(targets[ti]) <= lineno:
                hunks[targets[ti]]['label'] = last
                ti += 1
            if ti >= len(targets):
                break

    while ti < len(targets):
        hunks[targets[ti]]['label'] = last
        ti += 1

    return hunks


def side_by_side(hunk):
    rows = hunk['rows']
    out  = []
    i = 0
    while i < len(rows):
        tag = rows[i][0]
        if tag == 'ctx':
            _, text, old_no, new_no = rows[i]
            out.append(((old_no, text, 'ctx'), (new_no, text, 'ctx')))
            i += 1
        elif tag == 'note':
            i += 1
        else:
            dels = []
            adds = []
            while i < len(rows) and rows[i][0] == 'del':
                dels.append(rows[i])
                i += 1
            while i < len(rows) and rows[i][0] == 'add':
                adds.append(rows[i])
                i += 1
            for k in range(max(len(dels), len(adds))):
                left  = (dels[k][2], dels[k][1], 'del') if k < len(dels) else (None, '', 'empty')
                right = (adds[k][3], adds[k][1], 'add') if k < len(adds) else (None, '', 'empty')
                out.append((left, right))
    return out


def intraline(old, new):
    if len(old) > MAX_INTRALINE or len(new) > MAX_INTRALINE:
        return [(old, False)], [(new, False)]

    a = TOKEN_RE.findall(old)
    b = TOKEN_RE.findall(new)
    left  = []
    right = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk = False)
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op == 'equal':
            left.append((''.join(a[a1:a2]), False))
            right.append((''.join(b[b1:b2]), False))
        else:
            if a2 > a1:
                left.append((''.join(a[a1:a2]), True))
            if b2 > b1:
                right.append((''.join(b[b1:b2]), True))
    return left, right
