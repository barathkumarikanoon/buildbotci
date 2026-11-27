import io
import os
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.utils import OperationalError
from django.test import TestCase, override_settings

from ciweb.accounts import lockout, loginsession, superuser
from ciweb.runs.tests.test_views import PASSWORD, make_user


class LoginTest(TestCase):
    """The scripts in bin/ that change something let in website superusers only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = override_settings(RESULTS_DIR = self.tmp.name)
        patch.enable()
        self.addCleanup(patch.disable)
        env = mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.tmp.name, 'CI_ADMIN_USER': '', 'CI_ADMIN_PASSWORD': ''})
        env.start()
        self.addCleanup(env.stop)
        self.root = make_user('root', is_superuser = True, is_staff = True)

    def run_login(self, user = 'root', password = PASSWORD, script = 'workers', action = 'stop'):
        env = {'CI_ADMIN_USER': user, 'CI_ADMIN_PASSWORD': password}
        with mock.patch.dict(os.environ, env):
            call_command('ci_login', '--script', script, '--action', action, stderr = io.StringIO())

    def audit(self):
        with open(os.path.join(self.tmp.name, 'admin-actions.log')) as handle:
            return handle.read()

    def test_a_superuser_with_the_right_password_is_let_in_and_logged(self):
        self.run_login()
        line = self.audit().strip().splitlines()[-1]
        self.assertIn('root  workers stop  allowed', line)

    def test_the_name_can_be_the_email_in_any_letter_case(self):
        self.run_login(user = 'ROOT@Example.com')

    def test_a_wrong_password_is_refused_and_logged_without_the_password(self):
        with self.assertRaises(CommandError) as raised:
            self.run_login(password = 'not-the-password-1')
        self.assertIn('Wrong user name or password', str(raised.exception))
        self.assertIn('denied', self.audit())
        self.assertNotIn('not-the-password-1', self.audit())

    def test_an_unknown_name_gets_the_same_message(self):
        with self.assertRaises(CommandError) as raised:
            self.run_login(user = 'nobody')
        self.assertIn('Wrong user name or password', str(raised.exception))

    def test_a_staff_account_that_is_not_a_superuser_is_refused(self):
        make_user('helper', is_staff = True)
        with self.assertRaises(CommandError) as raised:
            self.run_login(user = 'helper')
        self.assertIn('not a superuser', str(raised.exception))

    def test_the_reason_is_only_given_after_the_right_password(self):
        make_user('helper', is_staff = True)
        with self.assertRaises(CommandError) as raised:
            self.run_login(user = 'helper', password = 'not-the-password-1')
        self.assertNotIn('superuser', str(raised.exception))

    def test_a_disabled_superuser_is_refused(self):
        get_user_model().objects.filter(username = 'root').update(is_active = False)
        make_user('second', is_superuser = True)
        with self.assertRaises(CommandError) as raised:
            self.run_login(user = 'root')
        self.assertIn('disabled', str(raised.exception))

    def test_five_wrong_passwords_lock_the_account_like_the_website(self):
        for _ in range(lockout.MAX_FAILURES):
            with self.assertRaises(CommandError):
                self.run_login(password = 'not-the-password-1')
        with self.assertRaises(CommandError) as raised:
            self.run_login()
        self.assertIn('Too many wrong attempts', str(raised.exception))

    def test_a_good_login_clears_earlier_failures(self):
        with self.assertRaises(CommandError):
            self.run_login(password = 'not-the-password-1')
        self.run_login()
        self.root.refresh_from_db()
        self.assertEqual(self.root.failed_logins, 0)

    def test_without_a_terminal_and_without_the_variables_nobody_is_let_in(self):
        with mock.patch.dict(os.environ, {'CI_ADMIN_USER': '', 'CI_ADMIN_PASSWORD': ''}), \
             mock.patch('sys.stdin.isatty', return_value = False):
            with self.assertRaises(CommandError) as raised:
                call_command('ci_login', stderr = io.StringIO())
        self.assertIn('CI_ADMIN_USER', str(raised.exception))

    def test_on_a_terminal_it_asks_for_both(self):
        with mock.patch.dict(os.environ, {'CI_ADMIN_USER': '', 'CI_ADMIN_PASSWORD': ''}), \
             mock.patch('sys.stdin.isatty', return_value = True), \
             mock.patch('builtins.input', return_value = 'root'), \
             mock.patch('getpass.getpass', return_value = PASSWORD):
            call_command('ci_login', '--script', 'master', '--action', 'update', stderr = io.StringIO())
        self.assertIn('root  master update  allowed', self.audit())

    def test_before_the_first_superuser_exists_nothing_is_asked(self):
        get_user_model().objects.all().delete()
        with mock.patch.dict(os.environ, {'CI_ADMIN_USER': '', 'CI_ADMIN_PASSWORD': ''}):
            call_command('ci_login', '--script', 'master', '--action', 'update', stderr = io.StringIO())
        self.assertIn('no superuser exists yet', self.audit())

    def test_a_database_problem_lets_nobody_in(self):
        with mock.patch('ciweb.accounts.superuser.state', return_value = superuser.UNREACHABLE):
            with self.assertRaises(CommandError) as raised:
                self.run_login()
        self.assertIn('database', str(raised.exception))

    def test_a_missing_table_counts_as_a_fresh_install_but_other_database_errors_do_not(self):
        with mock.patch.object(get_user_model().objects, 'filter', side_effect = OperationalError('no such table: ci_users')):
            self.assertEqual(superuser.state(), superuser.NOT_SET_UP)
        with mock.patch.object(get_user_model().objects, 'filter', side_effect = OperationalError('connection refused')):
            self.assertEqual(superuser.state(), superuser.UNREACHABLE)


class LoginSessionTest(TestCase):
    """After one login the scripts of the same terminal do not ask again."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patch in (override_settings(RESULTS_DIR = self.tmp.name),
                      mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.tmp.name, 'CI_ADMIN_USER': '', 'CI_ADMIN_PASSWORD': ''}),
                      mock.patch('sys.stdin.isatty', return_value = True)):
            patch.enable() if hasattr(patch, 'enable') else patch.start()
            self.addCleanup(patch.disable if hasattr(patch, 'enable') else patch.stop)
        self.root = make_user('root', is_superuser = True)
        self.asked = 0

    def run_login(self, *args, **answers):
        def ask(*_):
            self.asked += 1
            return answers.get('password', PASSWORD)
        out = io.StringIO()
        with mock.patch('builtins.input', return_value = answers.get('user', 'root')), mock.patch('getpass.getpass', side_effect = ask):
            call_command('ci_login', '--script', 'master', '--action', 'update', *args, stdout = out, stderr = io.StringIO())
        return out.getvalue()

    def test_the_second_script_does_not_ask(self):
        self.run_login()
        self.run_login()
        self.run_login()
        self.assertEqual(self.asked, 1)

    def test_every_use_is_still_logged(self):
        self.run_login()
        self.run_login()
        with open(os.path.join(self.tmp.name, 'admin-actions.log')) as handle:
            text = handle.read()
        self.assertIn('root  master update  allowed\n', text)
        self.assertIn('allowed (login of this terminal)', text)

    def test_the_login_is_private_to_the_account(self):
        self.run_login()
        self.assertEqual(oct(os.stat(loginsession.path()).st_mode & 0o777), '0o600')

    def test_it_runs_out_after_the_idle_time_and_each_use_renews_it(self):
        self.run_login()
        now = loginsession.time.time()
        with mock.patch.object(loginsession.time, 'time', return_value = now + 20 * 60):
            self.run_login()                                            # used after 20 minutes: renewed
        with mock.patch.object(loginsession.time, 'time', return_value = now + 40 * 60):
            self.run_login()                                            # 20 minutes after the last use: still there
        self.assertEqual(self.asked, 1)
        with mock.patch.object(loginsession.time, 'time', return_value = now + 80 * 60):
            self.run_login()                                            # 40 minutes unused: asks again
        self.assertEqual(self.asked, 2)

    def test_it_runs_out_after_eight_hours_however_often_it_is_used(self):
        self.run_login()
        now = loginsession.time.time()
        for minute in range(20, 8 * 60, 20):                              # used every 20 minutes for almost 8 hours
            with mock.patch.object(loginsession.time, 'time', return_value = now + minute * 60):
                self.run_login()
        self.assertEqual(self.asked, 1)
        with mock.patch.object(loginsession.time, 'time', return_value = now + 8 * 3600 + 120):
            self.run_login()                                              # 8 hours after the login: asks again, though just used
        self.assertEqual(self.asked, 2)

    def test_logging_out_ends_it(self):
        self.run_login()
        out = io.StringIO()
        call_command('ci_login', '--logout', stdout = out)
        self.assertIn('Logged out', out.getvalue())
        self.run_login()
        self.assertEqual(self.asked, 2)

    def test_another_terminal_has_its_own_login(self):
        self.run_login()
        with mock.patch('os.getsid', return_value = os.getsid(0) + 1):
            self.run_login()
        self.assertEqual(self.asked, 2)

    def test_it_ends_when_the_account_is_disabled_loses_the_right_or_changes_password(self):
        make_user('second', is_superuser = True)
        for change in (lambda u: setattr(u, 'is_active', False), lambda u: setattr(u, 'is_superuser', False),
                       lambda u: u.set_password('another-password-99z')):
            self.asked = 0
            self.root = get_user_model().objects.get(username = 'root')
            get_user_model().objects.filter(pk = self.root.pk).update(is_active = True, is_superuser = True)
            self.root.refresh_from_db()
            self.root.set_password(PASSWORD)
            self.root.save()
            self.run_login()
            self.root.refresh_from_db()
            change(self.root)
            self.root.save()
            with self.assertRaises(CommandError):
                self.run_login(password = PASSWORD) if self.root.check_password(PASSWORD) else self.run_login(password = 'wrong-password-1')
            self.assertEqual(self.asked, 2)

    def test_a_changed_file_is_not_believed(self):
        self.run_login()
        with open(loginsession.path(), 'a') as handle:
            handle.write('x')
        self.run_login()
        self.assertEqual(self.asked, 2)

    def test_a_job_with_the_variables_keeps_no_login(self):
        with mock.patch.dict(os.environ, {'CI_ADMIN_USER': 'root', 'CI_ADMIN_PASSWORD': PASSWORD}):
            call_command('ci_login', '--script', 'prune-runs', stderr = io.StringIO())
        self.assertFalse(os.path.exists(loginsession.path()))

    def test_sessions_can_be_switched_off(self):
        with override_settings(SCRIPT_LOGIN_MINUTES = 0):
            self.run_login()
            self.run_login()
        self.assertEqual(self.asked, 2)
        self.assertFalse(os.path.exists(loginsession.path()))

    def test_who_says_who_is_logged_in(self):
        self.assertIn('Nobody is logged in', self.run_login('--who'))
        self.run_login()
        self.assertIn('Logged in as root', self.run_login('--who'))

    def test_login_says_when_already_logged_in(self):
        self.run_login()
        self.assertIn('Already logged in as root', self.run_login('--login'))
        self.assertEqual(self.asked, 1)
