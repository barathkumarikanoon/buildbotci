import os
import subprocess
import sys
import tempfile
from unittest import mock

from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from ciweb import envfile
from ciweb.management.commands.runserver import Command, is_loopback


class EnvFileTest(SimpleTestCase):
    def test_it_reads_exports_quotes_comments_and_blank_lines(self):
        values = envfile.parse("# a comment\n\nexport A='one two'\nB=\"three\"\nexport C=four\n  export D = nope\nnot a setting\n")
        self.assertEqual(values, {'A': 'one two', 'B': 'three', 'C': 'four'})

    def test_it_expands_variables_the_way_a_shell_does(self):
        with mock.patch.dict(os.environ, {'HOME': '/home/me', 'WHO': 'x'}):
            values = envfile.parse("export A=\"$HOME/results\"\nexport B='$HOME/results'\nexport C=$HOME/results\nexport D=~/results\n")
        self.assertEqual(values, {'A': '/home/me/results', 'B': '$HOME/results', 'C': '/home/me/results', 'D': '/home/me/results'})

    def test_passwords_with_special_characters_survive(self):
        values = envfile.parse("export P='a$b!c d#e&f'\n")
        self.assertEqual(values['P'], 'a$b!c d#e&f')

    def test_loading_never_overrides_what_is_already_set_and_never_runs_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'env')
            with open(path, 'w') as handle:
                handle.write("export CIWEB_T_ONE='from file'\nexport CIWEB_T_TWO='from file'\nexport CIWEB_T_THREE=$(touch %s/ran)\n" % tmp)
            with mock.patch.dict(os.environ, {'CIWEB_T_ONE': 'already set'}):
                envfile.load(path)
                self.assertEqual(os.environ['CIWEB_T_ONE'], 'already set')
                self.assertEqual(os.environ['CIWEB_T_TWO'], 'from file')
                self.assertFalse(os.path.exists(os.path.join(tmp, 'ran')))
            os.environ.pop('CIWEB_T_TWO', None)
            os.environ.pop('CIWEB_T_THREE', None)

    def test_settings_already_set_in_the_terminal_that_differ_from_the_file_are_reported_by_name_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'env')
            with open(path, 'w') as handle:
                handle.write("export CIWEB_T_SAME='1'\nexport CIWEB_T_DIFF='from file'\nexport CIWEB_T_NEW='x'\n")
            with mock.patch.dict(os.environ, {'CIWEB_T_SAME': '1', 'CIWEB_T_DIFF': 'from terminal'}):
                envfile.load(path)
                self.assertEqual(envfile.CONFLICTS, ['CIWEB_T_DIFF'])
                self.assertEqual(os.environ['CIWEB_T_DIFF'], 'from terminal')
            os.environ.pop('CIWEB_T_NEW', None)
            envfile.load('/nonexistent/file.env')
            self.assertEqual(envfile.CONFLICTS, [])

    def test_a_missing_file_is_fine(self):
        self.assertEqual(envfile.load('/nonexistent/file.env'), {})


class RunserverTest(SimpleTestCase):
    def test_the_default_port_is_the_one_the_settings_expect(self):
        self.assertEqual(Command.default_port, '8020')

    def test_only_local_addresses_count_as_local(self):
        for addr in ('127.0.0.1', 'localhost', '::1', '[::1]', ''):
            self.assertTrue(is_loopback(addr), addr)
        for addr in ('0.0.0.0', '192.168.0.160', '10.1.2.3', 'example.com'):
            self.assertFalse(is_loopback(addr), addr)

    def command(self, addrport):
        with mock.patch('django.contrib.staticfiles.management.commands.runserver.Command.handle', return_value = 'started') as parent:
            return Command().handle(addrport = addrport), parent

    @override_settings(DEBUG = True)
    def test_debug_pages_are_never_exposed_beyond_this_machine(self):
        for addrport in ('0.0.0.0:8020', '192.168.0.160:8020', '0.0.0.0'):
            with self.assertRaisesMessage(CommandError, 'must not be exposed'):
                self.command(addrport)
        for addrport in ('127.0.0.1:8020', '8020', None, 'localhost:9000', '[::1]:8020'):
            result, parent = self.command(addrport)
            self.assertEqual(result, 'started')

    @override_settings(DEBUG = True, BRANCHES = ('main', '2026', 'buildbotci'))
    def test_it_says_which_repos_it_serves_and_warns_about_stale_terminal_settings(self):
        import io
        out = io.StringIO()
        with mock.patch.object(envfile, 'CONFLICTS', ['CI_BRANCHES', 'CI_UI_SECRET']), \
                mock.patch('django.contrib.staticfiles.management.commands.runserver.Command.inner_run', return_value = None):
            command = Command(stdout = out)
            command.inner_run(addrport = '127.0.0.1:8020')
        text = out.getvalue()
        self.assertIn('Repository profiles:', text)
        self.assertIn('CI_BRANCHES, CI_UI_SECRET', text)
        self.assertIn('unset CI_BRANCHES CI_UI_SECRET', text)

    @override_settings(DEBUG = False)
    def test_without_debug_any_address_is_allowed(self):
        self.assertEqual(self.command('0.0.0.0:8020')[0], 'started')


class SettingsTest(SimpleTestCase):
    def settings_for(self, argv, **extra):
        code = ("import sys; sys.argv = %r\nfrom ciweb import settings\n"
                "print(settings.DEBUG, settings.TESTING, settings.RUNSERVER)" % (argv,))
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CI_', 'BUILDBOT_'))}
        env.update(CI_UI_SECRET = 'x', CI_UI_DB_URL = 'sqlite://', CI_RESULTS_DIR = '/tmp/results', CI_BRANCHES = 'main', CI_ENV = '/nonexistent', PYTHONPATH = os.getcwd(), **extra)
        done = subprocess.run([sys.executable, '-c', code], env = env, stdout = subprocess.PIPE, stderr = subprocess.PIPE)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        return done.stdout.decode().split()

    def test_debug_is_on_only_for_the_development_server_unless_asked(self):
        self.assertEqual(self.settings_for(['manage.py', 'runserver']), ['True', 'False', 'True'])
        self.assertEqual(self.settings_for(['gunicorn']), ['False', 'False', 'False'])
        self.assertEqual(self.settings_for(['manage.py', 'migrate']), ['False', 'False', 'False'])
        self.assertEqual(self.settings_for(['manage.py', 'runserver'], CI_UI_DEBUG = '0')[0], 'False')
        self.assertEqual(self.settings_for(['gunicorn'], CI_UI_DEBUG = '1')[0], 'True')
