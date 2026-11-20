import inspect
import os
import subprocess

PASSED      = 'passed'
FAILED      = 'failed'
ERROR       = 'error'
NO_EXPECTED = 'no_expected'

CSV_TESTS = ('resolve_acts', 'resolve_judgments', 'resolve_debates',
             'resolve_articles', 'punishment')
XML_TESTS = ('judge_cite_extract', 'act_extract', 'article_extract',
             'debate_extract', 'legal_term_extract')
TXT_TESTS = ('cite_text', 'cite_law')


def output_filename(tocheck):
    if tocheck in CSV_TESTS:
        return '%s.csv' % tocheck
    if tocheck in XML_TESTS:
        return '%s.xml' % tocheck
    if tocheck in TXT_TESTS:
        return '%s.txt' % tocheck
    return tocheck


def compare_files(expectfile, outfile, labels = None):
    if not os.path.exists(expectfile):
        return NO_EXPECTED, ''
    cmd = ['diff', '-u']
    if labels:
        cmd.extend(['--label', labels[0], '--label', labels[1]])
    cmd.extend([expectfile, outfile])
    p = subprocess.run(cmd, stdout = subprocess.PIPE, stderr = subprocess.PIPE)
    if p.returncode == 0:
        return PASSED, ''
    if p.returncode == 1:
        return FAILED, p.stdout.decode('utf8', 'replace')
    raise RuntimeError(p.stderr.decode('utf8', 'replace'))


def get_class_tests(cls):
    names = []
    for name, member in inspect.getmembers(cls, inspect.isfunction):
        if not name.startswith('_'):
            names.append(name)
    return names
