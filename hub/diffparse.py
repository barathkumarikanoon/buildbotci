import bisect
import collections
import fnmatch
import re

HUNK_RE  = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: (.*))?')

MAX_LABEL     = 160


def parse_unified(text):
    hunks = []
    cur   = None
    old_no = new_no = 0

    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    for line in lines:
        m = HUNK_RE.match(line)
        if m:
            old_start = int(m.group(1))
            old_len   = 1 if m.group(2) is None else int(m.group(2))
            new_start = int(m.group(3))
            new_len   = 1 if m.group(4) is None else int(m.group(4))
            cur = {'old_start': old_start, 'old_len': old_len,
                   'new_start': new_start, 'new_len': new_len,
                   'rows': [], 'added': 0, 'removed': 0, 'label': (m.group(5) or '').strip()[:MAX_LABEL] or None,
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


def group_by_label(hunks):
    """Consecutive hunks that fall in the same document (same label) become one group, so a document
    that differs in several places gets one number. A hunk without a label stays a group of its own."""
    groups = []
    for index, hunk in enumerate(hunks):
        label = hunk.get('label')
        line  = hunk.get('change_line') or hunk.get('old_start')
        last  = groups[-1] if groups else None
        if last and label is not None and last['label'] == label:
            last['count']   += 1
            last['added']   += hunk['added']
            last['removed'] += hunk['removed']
        else:
            groups.append({'label': label, 'first': index, 'count': 1, 'line': line,
                           'added': hunk['added'], 'removed': hunk['removed']})
    return groups


def compile_formats(formats):
    """Rules that tell how an expected file is divided into records (documents, cases, ...).
    Each rule: `outputs` (file name globs), then either `start` (alternative runs of consecutive-line
    regular expressions that begin a record) or `key` (a regular expression whose group is the record's key:
    a new record begins when the key changes), and optionally `label` (a regular expression with a group,
    looked for in the first `label_lines` lines of the record; default is the record's first line)."""
    rules = []
    for rule in formats or []:
        start = [[re.compile(p) for p in sequence] for sequence in rule.get('start', []) if sequence]
        key   = re.compile(rule['key']) if rule.get('key') else None
        if not rule.get('outputs') or not (start or key):
            continue
        rules.append({'name': rule.get('name', ''), 'outputs': list(rule['outputs']), 'start': start, 'key': key,
                      'label': re.compile(rule['label']) if rule.get('label') else None,
                      'label_lines': int(rule.get('label_lines', 8))})
    return rules


def rule_for(output, rules):
    for rule in rules or []:
        if output and any(fnmatch.fnmatchcase(output, pattern) for pattern in rule['outputs']):
            return rule
    return None


def record_index(path, rule):
    """(starts, labels): the 1-based line where each record of the file begins, and its label."""
    sequences = rule['start']
    key       = rule['key']
    label_rx  = rule['label']
    width     = max([len(sequence) for sequence in sequences] or [1])
    window    = collections.deque(maxlen = width)
    starts, labels, firsts = [], [], {}
    waiting = []
    current = None

    def found(text):
        if label_rx:
            m = label_rx.search(text)
            if m:
                return (m.group(1) if m.groups() else m.group(0)).strip()[:MAX_LABEL]
        return None

    def begin(lineno, lines):
        starts.append(lineno)
        labels.append(None)
        index = len(starts) - 1
        firsts[index] = lines[0][1].strip()[:MAX_LABEL]
        for number, text in lines[:rule['label_lines']]:
            hit = found(text)
            if hit:
                labels[index] = hit
                return
        waiting.append((index, lineno + rule['label_lines'] - 1))

    with open(path, 'r', encoding = 'utf8', errors = 'replace', newline = '\n') as handle:
        for lineno, raw in enumerate(handle, 1):
            text = raw.rstrip('\r\n')
            window.append((lineno, text))
            if waiting:
                still = []
                for index, last in waiting:
                    hit = found(text)
                    if hit:
                        labels[index] = hit
                    elif lineno < last:
                        still.append((index, last))
                waiting = still
            if key:
                m = key.match(text)
                if m and m.group(1) != current:
                    current = m.group(1)
                    begin(lineno, [(lineno, text)])
                    labels[-1] = labels[-1] or current[:MAX_LABEL]
                continue
            begun = None
            for sequence in sequences:
                size = len(sequence)
                if len(window) < size or not sequence[-1].match(text):
                    continue
                tail = list(window)[-size:]
                if all(rx.match(t) for rx, (_, t) in zip(sequence, tail)):
                    first = lineno - size + 1
                    begun = first if begun is None else min(begun, first)
            if begun is not None and (not starts or begun > starts[-1]):
                begin(begun, [item for item in window if item[0] >= begun])
    for index, line in enumerate(labels):
        if line is None:
            labels[index] = firsts.get(index) or None
    return starts, labels


def label_by_records(hunks, starts, labels):
    for hunk in hunks:
        anchor = hunk['change_line'] or hunk['old_start']
        index  = bisect.bisect_right(starts, anchor) - 1
        hunk['label'] = labels[index] if index >= 0 else None
    return hunks


def format_unified(hunks):
    """Unified text of parsed hunks; a hunk's label follows the closing @@ like git's function context."""
    sign  = {'ctx': ' ', 'del': '-', 'add': '+'}
    lines = []
    for hunk in hunks:
        head = '@@ -%d,%d +%d,%d @@' % (hunk['old_start'], hunk['old_len'], hunk['new_start'], hunk['new_len'])
        lines.append(head + (' ' + hunk['label'] if hunk.get('label') else ''))
        for tag, text, _, _ in hunk['rows']:
            lines.append(text if tag == 'note' else sign[tag] + text)
    return '\n'.join(lines) + '\n' if lines else ''


def _piece_text(rows, old_next, new_next):
    old_rows = [r for r in rows if r[2] is not None]
    new_rows = [r for r in rows if r[3] is not None]
    old_start = old_rows[0][2] if old_rows else old_next - 1
    new_start = new_rows[0][3] if new_rows else new_next - 1
    sign  = {'ctx': ' ', 'del': '-', 'add': '+'}
    lines = ['@@ -%d,%d +%d,%d @@' % (old_start, len(old_rows), new_start, len(new_rows))]
    for tag, text, _, _ in rows:
        lines.append(text if tag == 'note' else sign[tag] + text)
    return lines


def split_at_documents(hunks, starts):
    """Unified text of the hunks, cut at document boundaries, so that one hunk never mixes two documents.
    Context that belongs to a neighbouring document (for example the header of the next document) is dropped."""
    out = []
    for hunk in hunks:
        rows = hunk['rows']
        docs = []
        for tag, text, old_no, new_no in rows:
            if old_no is not None:
                docs.append(bisect.bisect_right(starts, old_no))
            else:
                docs.append(docs[-1] if docs else None)
        first = next((d for d in docs if d is not None), 0)
        docs  = [first if d is None else d for d in docs]

        old_next = hunk['old_start'] if hunk['old_len'] else hunk['old_start'] + 1
        new_next = hunk['new_start'] if hunk['new_len'] else hunk['new_start'] + 1
        index = 0
        while index < len(rows):
            end = index
            while end < len(rows) and docs[end] == docs[index]:
                end += 1
            piece = rows[index:end]
            if any(r[0] in ('add', 'del') for r in piece):
                out.extend(_piece_text(piece, old_next, new_next))
            for tag, text, old_no, new_no in piece:
                if old_no is not None:
                    old_next = old_no + 1
                if new_no is not None:
                    new_next = new_no + 1
            index = end
    return '\n'.join(out) + '\n' if out else ''


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

    with open(expected_path, 'r', encoding = 'utf8', errors = 'replace', newline = '\n') as handle:
        for lineno, line in enumerate(handle, 1):
            line = line.rstrip('\r\n')
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
