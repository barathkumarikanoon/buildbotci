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

    def test_intraline_marks_only_changed_words(self):
        left, right = diffparse.intraline('the quick brown fox', 'the quick red fox')
        self.assertEqual([t for t, c in left if c], ['brown'])
        self.assertEqual([t for t, c in right if c], ['red'])


if __name__ == '__main__':
    unittest.main()
