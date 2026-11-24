import os
import subprocess
import tempfile
import unittest

from hub import diffparse

DIFF = """--- expected/x
+++ actual/x
@@ -2,3 +2,3 @@
 keep
-old line
+new line
 tail
@@ -20,2 +20,3 @@
 a
+inserted
 b
"""


class ParseTest(unittest.TestCase):
    def test_hunks_and_counts(self):
        hunks = diffparse.parse_unified(DIFF)
        self.assertEqual(len(hunks), 2)
        self.assertEqual((hunks[0]['added'], hunks[0]['removed']), (1, 1))
        self.assertEqual((hunks[1]['added'], hunks[1]['removed']), (1, 0))
        stats = diffparse.summarize(hunks)
        self.assertEqual(stats, {'hunks': 2, 'added': 2, 'removed': 1, 'first_line': 3})

    def test_line_numbers(self):
        rows = diffparse.parse_unified(DIFF)[0]['rows']
        self.assertEqual(rows[0], ('ctx', 'keep', 2, 2))
        self.assertEqual(rows[1], ('del', 'old line', 3, None))
        self.assertEqual(rows[2], ('add', 'new line', None, 3))

    def test_removed_line_starting_with_dashes_is_content(self):
        text = '--- a\n+++ b\n@@ -1,2 +1,1 @@\n--- not a header\n keep\n'
        hunk = diffparse.parse_unified(text)[0]
        self.assertEqual(hunk['removed'], 1)
        self.assertEqual(hunk['rows'][0][1], '-- not a header')

    def test_labels_use_nearest_preceding_marker(self):
        lines = ['RELURL: doc/1'] + ['x'] * 10 + ['RELURL: doc/2'] + ['y'] * 20
        with tempfile.NamedTemporaryFile('w', delete = False, suffix = '.txt') as handle:
            handle.write('\n'.join(lines) + '\n')
            path = handle.name
        hunks = [{'old_start': 5, 'change_line': 5, 'label': None},
                 {'old_start': 17, 'change_line': 20, 'label': None}]
        diffparse.label_hunks(hunks, path, diffparse.compile_markers([r'^RELURL:\s*(.+)$']))
        self.assertEqual([h['label'] for h in hunks], ['doc/1', 'doc/2'])

    def test_side_by_side_pairs_changes(self):
        hunk = diffparse.parse_unified(DIFF)[0]
        rows = diffparse.side_by_side(hunk)
        self.assertEqual(rows[1][0][2], 'del')
        self.assertEqual((rows[1][0][1], rows[1][1][1]), ('old line', 'new line'))
        insert = diffparse.side_by_side(diffparse.parse_unified(DIFF)[1])
        self.assertEqual(insert[1][0][2], 'empty')
        self.assertEqual(insert[1][1][2], 'add')


class GroupTest(unittest.TestCase):
    def hunk(self, label, added = 1, removed = 1, line = 5):
        return {'label': label, 'added': added, 'removed': removed, 'change_line': line, 'old_start': line}

    def test_hunks_of_one_document_share_a_group_and_a_number(self):
        groups = diffparse.group_by_label([self.hunk('a', 1, 2, 9), self.hunk('a', 3, 4, 17), self.hunk('b', 1, 1, 30)])
        self.assertEqual([(g['label'], g['first'], g['count'], g['added'], g['removed'], g['line']) for g in groups],
                         [('a', 0, 2, 4, 6, 9), ('b', 2, 1, 1, 1, 30)])

    def test_unlabelled_hunks_and_a_document_that_comes_back_stay_separate(self):
        groups = diffparse.group_by_label([self.hunk(None), self.hunk(None), self.hunk('a'), self.hunk('b'), self.hunk('a')])
        self.assertEqual([g['count'] for g in groups], [1, 1, 1, 1, 1])

    def test_no_hunks_no_groups(self):
        self.assertEqual(diffparse.group_by_label([]), [])


def doc(n, body):
    return ['=' * 12, 'docid: %d' % n, 'RELURL: d/%d.pdf' % n, '=' * 12] + body


LAYOUT = {'outputs': ['*'], 'label': '^RELURL:\\s*(.+)$',
          'start': [['^={10,}$', '^docid:', '^RELURL:'], ['^docid:', '^RELURL:']]}


class RecordRuleTest(unittest.TestCase):
    def write(self, lines):
        handle = tempfile.NamedTemporaryFile('w', suffix = '.txt', delete = False, encoding = 'utf8', newline = '')
        handle.write('\n'.join(lines) + '\n')
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def rule(self, **spec):
        return diffparse.compile_formats([dict(spec, outputs = spec.get('outputs', ['*']))])[0]

    def diff_of(self, old, new):
        a, b = self.write(old), self.write(new)
        out = subprocess.run(['diff', '-U3', a, b], stdout = subprocess.PIPE).stdout.decode()
        return out[out.index('@@'):]

    def split(self, old, new, rule):
        path = self.write(old)
        starts, labels = diffparse.record_index(path, rule)
        hunks = diffparse.parse_unified(diffparse.split_at_documents(diffparse.parse_unified(self.diff_of(old, new)), starts))
        return diffparse.label_by_records(hunks, starts, labels), starts, labels

    def test_start_sequences_give_the_first_line_and_the_label_of_each_record(self):
        lines = ['docid: 1', 'RELURL: first', '=' * 12, 'text'] + doc(2, ['x']) + doc(3, ['y'])
        starts, labels = diffparse.record_index(self.write(lines), self.rule(**LAYOUT))
        self.assertEqual((starts, labels), ([1, 5, 10], ['first', 'd/2.pdf', 'd/3.pdf']))

    def test_the_label_defaults_to_the_first_line_of_the_record(self):
        starts, labels = diffparse.record_index(self.write(['1 a | b', 'bench: x', '2 c | d']), self.rule(start = [['^\\d+ .+ \\| .+$']]))
        self.assertEqual((starts, labels), ([1, 3], ['1 a | b', '2 c | d']))

    def test_a_key_rule_starts_a_record_when_the_key_changes(self):
        lines = ['docid,x', '10,a', '10,b', 'continued line of b', '11,c', '10,d']
        starts, labels = diffparse.record_index(self.write(lines), self.rule(key = '^(\\d+),'))
        self.assertEqual((starts, labels), ([2, 5, 6], ['10', '11', '10']))

    def test_a_line_rule_makes_every_line_a_record(self):
        starts, labels = diffparse.record_index(self.write(['a', '', 'b', 'c']), self.rule(start = [['^.+$']]))
        self.assertEqual(starts, [1, 3, 4])

    def test_the_label_is_searched_in_the_first_lines_of_the_record(self):
        rule = self.rule(start = [['^TEST$']], label = '^(?!TEST$)(.+)$')
        starts, labels = diffparse.record_index(self.write(['TEST', 'query one', 'out', 'TEST', 'query two']), rule)
        self.assertEqual(labels, ['query one', 'query two'])

    def test_rules_are_chosen_by_the_output_file_name(self):
        rules = diffparse.compile_formats([{'outputs': ['*_hdr', 'x.csv'], 'start': [['^a']]}, {'outputs': ['y'], 'key': '^(.)'}])
        self.assertEqual(diffparse.rule_for('delhi_hdr', rules), rules[0])
        self.assertEqual(diffparse.rule_for('y', rules), rules[1])
        self.assertIsNone(diffparse.rule_for('other', rules))
        self.assertIsNone(diffparse.rule_for(None, rules))

    def test_a_hunk_is_cut_where_one_document_ends_and_the_next_begins(self):
        old = doc(1, ['a', 'b', 'c', 'old1']) + doc(2, ['d', 'old2', 'e'])
        new = doc(1, ['a', 'b', 'c', 'new1']) + doc(2, ['d', 'new2', 'e'])
        self.assertEqual(len(diffparse.parse_unified(self.diff_of(old, new))), 1)
        pieces, starts, labels = self.split(old, new, self.rule(**LAYOUT))
        self.assertEqual([(h['added'], h['removed'], h['label']) for h in pieces], [(1, 1, 'd/1.pdf'), (1, 1, 'd/2.pdf')])
        self.assertNotIn('docid: 2', [r[1] for r in pieces[0]['rows']])
        self.assertIn('docid: 2', [r[1] for r in pieces[1]['rows']])

    def test_the_next_documents_header_is_dropped_when_only_it_is_context(self):
        old = doc(1, ['a', 'b', 'old']) + doc(2, ['x', 'y', 'z'])
        new = doc(1, ['a', 'b', 'new']) + doc(2, ['x', 'y', 'z'])
        pieces, _, _ = self.split(old, new, self.rule(**LAYOUT))
        self.assertEqual(len(pieces), 1)
        self.assertNotIn('docid: 2', [r[1] for r in pieces[0]['rows']])

    def test_changes_and_line_numbers_are_kept_exactly(self):
        old = doc(1, ['a', 'b', 'c', 'old1', 'p', 'q']) + doc(2, ['d', 'old2', 'e', 'f'])
        new = doc(1, ['a', 'b', 'c', 'new1', 'p', 'q']) + doc(2, ['d', 'new2', 'e', 'f'])
        hunks = diffparse.parse_unified(self.diff_of(old, new))
        pieces, _, _ = self.split(old, new, self.rule(**LAYOUT))
        changes = lambda hs: [r for h in hs for r in h['rows'] if r[0] in ('add', 'del')]
        self.assertEqual(changes(pieces), changes(hunks))

    def test_a_pure_insertion_in_a_later_document_keeps_a_valid_header(self):
        old = doc(1, ['a', 'b']) + doc(2, ['c', 'd'])
        new = doc(1, ['a', 'b']) + doc(2, ['c', 'NEW', 'd'])
        pieces, _, _ = self.split(old, new, self.rule(**LAYOUT))
        self.assertEqual([(h['added'], h['removed']) for h in pieces], [(1, 0)])
        self.assertEqual([r for r in pieces[0]['rows'] if r[0] == 'add'][0][1], 'NEW')

    def test_a_hunk_inside_one_document_is_left_as_it_was(self):
        old = doc(1, ['a', 'b', 'c', 'old', 'd', 'e'])
        new = doc(1, ['a', 'b', 'c', 'new', 'd', 'e'])
        hunks = diffparse.parse_unified(self.diff_of(old, new))
        pieces, _, _ = self.split(old, new, self.rule(**LAYOUT))
        shape = lambda hs: [(h['old_start'], h['old_len'], h['new_start'], h['new_len'], h['rows']) for h in hs]
        self.assertEqual(shape(pieces), shape(hunks))

    def test_labels_travel_in_the_hunk_header_and_come_back(self):
        old = doc(1, ['a', 'old']) + doc(2, ['b', 'old2'])
        new = doc(1, ['a', 'new']) + doc(2, ['b', 'new2'])
        pieces, _, _ = self.split(old, new, self.rule(**LAYOUT))
        text = diffparse.format_unified(pieces)
        self.assertRegex(text, r'(?m)^@@ -\d+,\d+ \+\d+,\d+ @@ d/1\.pdf$')
        again = diffparse.parse_unified(text)
        self.assertEqual([h['label'] for h in again], ['d/1.pdf', 'd/2.pdf'])
        self.assertEqual(diffparse.format_unified(again), text)

    def test_text_with_carriage_returns_and_form_feeds_survives_a_round_trip(self):
        body = '@@ -1,2 +1,2 @@\n a\rb\x0cc\n-old\x1cline\n+new\u2028line\n'
        self.assertEqual(diffparse.format_unified(diffparse.parse_unified(body)), body)


if __name__ == '__main__':
    unittest.main()
