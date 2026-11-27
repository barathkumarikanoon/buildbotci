"""Making a readable difference out of two output files: git's histogram diff, cut at record boundaries and labelled.

Used by every adapter that judges a test by comparing its output with an expected file."""
import os
import re
import subprocess

from hub import diffparse

MAX_HUNK_SUMMARY = 1000


def human_size(count):
    value = float(count)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return ('%d %s' % (value, unit)) if unit == 'B' else ('%.1f %s' % (value, unit))
        value /= 1024.0


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None

GIT_DIFF_TIMEOUT = 600


def histogram_diff(expected_path, actual_path, labels, cwd):
    """Unified diff of two files made by `git diff --histogram`, with the same two header lines as
    `diff -u --label`. Returns None when git cannot make it, so the caller keeps the diff it already has."""
    env = {'PATH': os.environ.get('PATH', os.defpath), 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_PAGER': 'cat', 'HOME': os.path.expanduser('~')}
    expected_path = os.path.abspath(expected_path)
    actual_path   = os.path.abspath(actual_path)
    try:
        run = subprocess.run(
            ['git', 'diff', '--no-index', '--histogram', '--no-color', '--no-ext-diff', '--no-renames',
             '--text', '--no-prefix', '--unified=3', '--', expected_path, actual_path],
            cwd = cwd, env = env, stdout = subprocess.PIPE, stderr = subprocess.DEVNULL, timeout = GIT_DIFF_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode != 1:
        return None
    text  = run.stdout.decode('utf8', 'replace')
    start = 0 if text.startswith('@@') else text.find('\n@@') + 1
    if start == 0 and not text.startswith('@@'):
        return None
    body = re.sub(r'^(@@ [^@\n]*@@).*$', r'\1', text[start:], flags = re.M)
    return '--- %s\n+++ %s\n%s' % (labels[0], labels[1], body)


def analyze_failure(difftext, expected_path, markers, rule = None):
    """Returns (diff text, stats, summary). With a record rule for the output file, every hunk is cut at the
    record (document) boundaries of the expected file and labelled with its record; the label follows the
    closing @@ of each hunk in the returned text. Without a rule, labels come from the context markers."""
    head  = difftext[:difftext.find('\n@@') + 1] if '\n@@' in difftext else ''
    hunks = diffparse.parse_unified(difftext)
    starts = labels = None
    if rule and hunks:
        starts, labels = diffparse.record_index(expected_path, rule)
    if starts:
        hunks = diffparse.parse_unified(diffparse.split_at_documents(hunks, starts))
        diffparse.label_by_records(hunks, starts, labels)
        difftext = head + diffparse.format_unified(hunks)
    else:
        diffparse.label_hunks(hunks, expected_path, markers)
        difftext = head + diffparse.format_unified(hunks) if hunks else difftext
    stats = diffparse.summarize(hunks)
    summary = [{'old_start': h['old_start'], 'new_start': h['new_start'],
                'line': h['change_line'] or h['old_start'],
                'added': h['added'], 'removed': h['removed'], 'label': h['label']}
               for h in hunks[:MAX_HUNK_SUMMARY]]
    labelled = []
    for h in hunks:
        if h['label'] and h['label'] not in labelled:
            labelled.append(h['label'])
    stats['labels'] = len(labelled)
    stats['documents'] = len(diffparse.group_by_label(hunks))
    return difftext, stats, summary


def describe_diff(stats, summary):
    text = 'Output differs from expected: %d hunk%s, +%d / -%d lines, first difference at expected line %s' % (
        stats['hunks'], '' if stats['hunks'] == 1 else 's', stats['added'],
        stats['removed'], stats['first_line'])
    if summary and summary[0]['label']:
        text += ' (near: %s)' % summary[0]['label']
    if stats.get('labels', 0) > 1:
        text += '; %d distinct documents/sections affected' % stats['labels']
    return text
