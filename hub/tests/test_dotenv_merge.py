import contextlib
import io
import os
import subprocess
import tempfile
import unittest

from hub import dotenv_merge as profile
from hub.commands import ops
from hub.tests import fixtures

COMMITTED = """# settings of a developer
DATADIR=/home/someone/indianlaw_data
LAWDB_NAME=lawdb
LAWDB_HOST=local
export TIDDB_HOST=local
EMPTY=
"""


class MergeTest(unittest.TestCase):
    def test_only_the_listed_settings_change_and_everything_else_is_kept_in_place(self):
        merged, applied = profile.merge(COMMITTED, "DATADIR=/home/me/indianlaw_data\n")
        self.assertEqual(applied, ['DATADIR'])
        lines = merged.splitlines()
        self.assertEqual(lines[0], '# settings of a developer')
        self.assertEqual(lines[1], 'DATADIR=/home/me/indianlaw_data')
        self.assertEqual(lines[2:], ['LAWDB_NAME=lawdb', 'LAWDB_HOST=local', 'export TIDDB_HOST=local', 'EMPTY='])

    def test_a_setting_that_is_not_there_yet_is_added_at_the_end(self):
        merged, applied = profile.merge(COMMITTED, "NEW_SETTING=1\n")
        self.assertEqual(applied, ['NEW_SETTING'])
        self.assertEqual(merged.splitlines()[-1], 'NEW_SETTING=1')
        self.assertIn('DATADIR=/home/someone/indianlaw_data', merged)

    def test_it_understands_export_comments_quotes_and_blank_lines(self):
        merged, applied = profile.merge(COMMITTED, "# mine\n\nexport LAWDB_HOST='db.example'\nDATADIR=\"/d d\"\n")
        self.assertEqual(sorted(applied), ['DATADIR', 'LAWDB_HOST'])
        self.assertIn("LAWDB_HOST='db.example'", merged)
        self.assertIn('DATADIR="/d d"', merged)
        self.assertNotIn('export LAWDB_HOST', merged)

    def test_a_commented_out_setting_is_not_touched_and_values_with_equals_signs_survive(self):
        merged, applied = profile.merge("# DATADIR=/old\nTOKEN=a=b==\n", "TOKEN=c=d==\nDATADIR=/new\n")
        self.assertIn('# DATADIR=/old', merged)
        self.assertEqual(merged.splitlines(), ['# DATADIR=/old', 'TOKEN=c=d==', 'DATADIR=/new'])

    def test_applying_the_same_file_twice_changes_nothing_the_second_time(self):
        once, first = profile.merge(COMMITTED, "DATADIR=/home/me/d\n")
        twice, second = profile.merge(once, "DATADIR=/home/me/d\n")
        self.assertEqual((once, first, second), (twice, ['DATADIR'], []))

    def test_an_empty_override_file_leaves_the_settings_alone(self):
        merged, applied = profile.merge(COMMITTED, "# nothing\n")
        self.assertEqual((merged, applied), (COMMITTED, []))


class PrepareCommandTest(unittest.TestCase):
    """`runner.py prepare` of an indianlaw repo profile: merging the machine's own settings file into the checkout."""

    def setUp(self):
        self.tmp  = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = os.path.join(self.tmp.name, 'indianlaw')
        os.makedirs(self.tree)
        subprocess.run(['git', '-C', self.tree, 'init', '-q'], check = True)
        with open(os.path.join(self.tree, '.env.local'), 'w') as handle:
            handle.write(COMMITTED)
        self.results = os.path.join(self.tmp.name, 'results')

    def run_command(self, text, name = 'mine.env', env_profile = 'local', file = None):
        path = file or os.path.join(self.tmp.name, name)
        if file is None:
            with open(path, 'w') as handle:
                handle.write(text)
        config = fixtures.repo_config(self.tmp.name, repo_dir = self.tree, options = {'env_profile': env_profile, 'profile_file': path})
        fixtures.write_profiles(self.results, config)
        args = fixtures.run_args(self.results, tree = self.tree)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ops.cmd_prepare(args)
        return code, out.getvalue()

    def test_it_merges_into_the_checkout_and_reports_names_only(self):
        code, text = self.run_command("DATADIR=/home/me/indianlaw_data\nSECRET_TOKEN=very-secret\n")
        self.assertEqual(code, 0)
        self.assertIn('DATADIR, SECRET_TOKEN', text)
        self.assertNotIn('very-secret', text)
        self.assertNotIn('/home/me', text)
        with open(os.path.join(self.tree, '.env.local')) as handle:
            content = handle.read()
        self.assertIn('DATADIR=/home/me/indianlaw_data', content)
        self.assertIn('LAWDB_HOST=local', content)
        self.assertIn('SECRET_TOKEN=very-secret', content)

    def test_a_missing_file_is_reported_clearly(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_command('', file = '/nonexistent/mine.env')
        self.assertIn('Settings file not found', str(caught.exception))

    def test_a_profile_without_a_file_in_the_checkout_is_created(self):
        self.run_command("DATADIR=/d\n", env_profile = 'ikoffice')
        with open(os.path.join(self.tree, '.env.ikoffice')) as handle:
            self.assertEqual(handle.read(), 'DATADIR=/d\n')

    def test_only_suites_that_need_it_get_a_prepare_step(self):
        from hub.adapters import adapter_class
        self.assertTrue(adapter_class('comparison').has_prepare({'profile_file': '/x'}))
        self.assertFalse(adapter_class('comparison').has_prepare({'profile_file': ''}))
        self.assertFalse(adapter_class('command').has_prepare({}))


if __name__ == '__main__':
    unittest.main()
