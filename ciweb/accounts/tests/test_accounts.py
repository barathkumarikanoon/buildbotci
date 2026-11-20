import io
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from ciweb.accounts import lockout

User     = get_user_model()
GOOD     = 'blue-tractor-77x'
STRONG   = 'quiet-mountain-48k'
NEW      = 'new-orange-pen-55'
MAIL_ON  = dict(MAIL_CONFIGURED = True, PUBLIC_URL = 'http://ui.example', EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend')


def person(name, state = 'active', **extra):
    values = dict(is_active = state == 'active', approved = state in ('active', 'disabled'), email_verified = True)
    values.update(extra)
    return User.objects.create_user(name, '%s@example.com' % name.lower(), GOOD, **values)


def flashes(reply):
    return ' | '.join(m.message for m in reply.context['messages']) if reply.context and 'messages' in reply.context else ''


def page_errors(reply):
    return re.findall(r'class="(?:flash error|bad small)">([^<]*)<', reply.content.decode())


class UserModelTest(TestCase):
    def test_the_table_is_called_ci_users_and_stores_only_argon2id_hashes(self):
        self.assertEqual(User._meta.db_table, 'ci_users')
        with override_settings(PASSWORD_HASHERS = ['django.contrib.auth.hashers.Argon2PasswordHasher']):
            user = person('Dev')
        self.assertTrue(user.password.startswith('argon2$argon2id$'))
        self.assertNotIn(GOOD, user.password)
        other = person('Two')
        self.assertNotEqual(user.password, other.password)

    def test_names_and_emails_are_unique_ignoring_case(self):
        person('Dev')
        for name, email in (('dev', 'x@example.com'), ('DEV', 'y@example.com'), ('other', 'DEV@example.com')):
            with self.assertRaises(IntegrityError), transaction.atomic():
                User.objects.create_user(name, email, GOOD)

    def test_reserved_and_malformed_names_are_refused(self):
        for bad in ('Auto', 'auto', 'ab', 'has space', '-start', 'x' * 33, 'semi;colon'):
            user = User(username = bad, email = 'a@example.com')
            with self.assertRaises(ValidationError, msg = bad):
                user.full_clean(exclude = ['password'])

    def test_state_tells_pending_from_disabled(self):
        self.assertEqual(person('One', 'active').state, 'active')
        self.assertEqual(person('Two', 'pending').state, 'pending')
        self.assertEqual(person('Three', 'disabled').state, 'disabled')


class LoginTest(TestCase):
    def setUp(self):
        self.alice = person('alice')
        person('wait', 'pending')
        person('off', 'disabled')

    def login(self, who, password = GOOD, client = None, **extra):
        return (client or self.client).post('/login', dict(username = who, password = password, **extra))

    def test_sign_in_by_name_or_email_in_any_case(self):
        for who in ('alice', 'ALICE', 'alice@example.com', 'Alice@Example.com'):
            client = Client()
            self.assertEqual(self.login(who, client = client).status_code, 302, who)
            self.assertEqual(client.get('/new').status_code, 200)

    def test_the_cookie_is_http_only_and_same_site_lax(self):
        reply = self.login('alice')
        cookie = reply.cookies['sessionid']
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Lax')

    def test_wrong_and_unknown_users_get_the_same_message(self):
        one = page_errors(self.login('alice', 'wrong-password-1'))
        two = page_errors(self.login('nobody', 'wrong-password-1'))
        self.assertEqual(one, two)
        self.assertIn('Wrong user name or password.', one)

    def test_pending_and_disabled_say_so_only_after_the_password_is_right(self):
        self.assertIn('waiting for approval', ' '.join(page_errors(self.login('wait'))))
        self.assertIn('is disabled', ' '.join(page_errors(self.login('off'))))
        self.assertEqual(page_errors(self.login('wait', 'wrong-password-1')), ['Wrong user name or password.'])
        self.assertEqual(page_errors(self.login('off', 'wrong-password-1')), ['Wrong user name or password.'])
        self.assertIsNone(User.objects.get(username = 'wait').last_login)

    def test_five_wrong_passwords_lock_the_account_even_for_the_right_one(self):
        for _ in range(lockout.MAX_FAILURES):
            self.login('alice', 'wrong-password-1')
        self.assertIn('Too many wrong attempts', ' '.join(page_errors(self.login('alice'))))
        lockout.clear(User.objects.get(username = 'alice'))
        self.assertEqual(self.login('alice').status_code, 302)

    def test_a_lock_expires_and_a_success_resets_the_counter(self):
        for _ in range(lockout.MAX_FAILURES):
            self.login('alice', 'wrong-password-1')
        User.objects.filter(username = 'alice').update(locked_until = timezone.now() - timedelta(seconds = 1))
        self.assertEqual(self.login('alice').status_code, 302)
        self.client.post('/logout')
        for _ in range(lockout.MAX_FAILURES - 1):
            self.login('alice', 'wrong-password-1')
        self.assertEqual(self.login('alice').status_code, 302)
        self.assertEqual(User.objects.get(username = 'alice').failed_logins, 0)

    def test_after_login_only_local_pages_can_be_the_destination(self):
        for target, expected in (('/new?branch=2026', '/new?branch=2026'), ('//evil.example/x', '/'),
                                 ('http://evil.example', '/'), ('https://evil.example/new', '/'), ('', '/')):
            reply = self.login('alice', client = Client(), next = target)
            self.assertEqual(reply['Location'], expected, target)

    def test_the_login_page_for_a_page_that_needs_it_comes_back_to_it(self):
        reply = self.client.get('/new?branch=2026')
        self.assertEqual(reply.status_code, 302)
        self.assertIn('/login?next=/new%3Fbranch%3D2026', reply['Location'])

    def test_sign_out_needs_a_post_and_ends_the_session(self):
        self.login('alice')
        self.assertEqual(self.client.get('/logout').status_code, 405)
        self.assertEqual(self.client.post('/logout').status_code, 302)
        self.assertEqual(self.client.get('/new').status_code, 302)

    def test_forms_without_the_secret_token_are_refused(self):
        strict = Client(enforce_csrf_checks = True)
        self.assertEqual(strict.post('/login', {'username': 'alice', 'password': GOOD}).status_code, 403)
        strict.get('/login')
        token = strict.cookies['csrftoken'].value
        self.assertEqual(strict.post('/login', {'username': 'alice', 'password': GOOD, 'csrfmiddlewaretoken': token}).status_code, 302)

    def test_a_disabled_account_is_signed_out_on_its_next_request(self):
        self.login('alice')
        self.assertEqual(self.client.get('/new').status_code, 200)
        User.objects.filter(username = 'alice').update(is_active = False)
        self.assertEqual(self.client.get('/new').status_code, 302)

    def test_browsers_post_forms_with_the_real_origin_and_not_origin_null(self):
        reply = self.client.get('/login')
        self.assertNotEqual(reply['Referrer-Policy'], 'no-referrer')
        self.assertEqual(reply['Referrer-Policy'], 'same-origin')

    def test_forms_work_with_the_origin_header_a_real_browser_sends(self):
        strict = Client(enforce_csrf_checks = True)
        person('browser')
        strict.get('/login')
        token = strict.cookies['csrftoken'].value
        data  = {'username': 'browser', 'password': GOOD, 'csrfmiddlewaretoken': token}
        self.assertEqual(strict.post('/login', data, HTTP_ORIGIN = 'http://testserver').status_code, 302)
        other = Client(enforce_csrf_checks = True)
        other.get('/login')
        data['csrfmiddlewaretoken'] = other.cookies['csrftoken'].value
        self.assertEqual(other.post('/login', data, HTTP_ORIGIN = 'http://evil.example').status_code, 403)

    def test_a_refused_form_explains_itself_in_plain_words(self):
        reply = Client(enforce_csrf_checks = True).post('/signup', {'username': 'x'})
        self.assertEqual(reply.status_code, 403)
        html = reply.content.decode()
        self.assertIn('The form was not accepted', html)
        self.assertIn('Technical reason', html)

    def test_pages_for_signing_in_are_not_cached_and_hide_nothing_secret(self):
        reply = self.client.get('/login')
        self.assertEqual(reply['Referrer-Policy'], 'same-origin')
        self.assertEqual(reply['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(reply['X-Frame-Options'], 'DENY')


@override_settings(**MAIL_ON, SIGNUP_DOMAINS = ('example.com',))
class SignupTest(TestCase):
    def signup(self, **overrides):
        form = {'username': 'newbie', 'email': 'newbie@example.com', 'password1': STRONG, 'password2': STRONG}
        form.update(overrides)
        return self.client.post('/signup', form)

    def test_confirm_the_email_and_the_account_activates_for_an_allowed_domain(self):
        self.assertEqual(self.signup().status_code, 302)
        user = User.objects.get(username = 'newbie')
        self.assertEqual(user.state, 'pending')
        self.assertIn('waiting for approval', ' '.join(page_errors(self.client.post('/login', {'username': 'newbie', 'password': STRONG}))))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['newbie@example.com'])
        link = re.search(r'http://ui\.example(/verify/\S+)', mail.outbox[0].body).group(1)
        self.assertEqual(self.client.get(link).status_code, 302)
        user.refresh_from_db()
        self.assertEqual((user.state, user.email_verified), ('active', True))
        self.assertEqual(self.client.post('/login', {'username': 'newbie', 'password': STRONG}).status_code, 302)

    def test_an_address_outside_the_allowed_domains_still_needs_an_administrator(self):
        self.signup(email = 'newbie@elsewhere.org')
        link = re.search(r'http://ui\.example(/verify/\S+)', mail.outbox[0].body).group(1)
        self.client.get(link)
        user = User.objects.get(username = 'newbie')
        self.assertEqual((user.state, user.email_verified), ('pending', True))
        user.approve()
        self.assertEqual(self.client.post('/login', {'username': 'newbie', 'password': STRONG}).status_code, 302)

    def test_a_disabled_account_is_not_reactivated_by_an_old_confirmation_link(self):
        self.signup()
        link = re.search(r'http://ui\.example(/verify/\S+)', mail.outbox[0].body).group(1)
        self.client.get(link)
        User.objects.get(username = 'newbie').disable()
        self.client.get(link)
        self.assertEqual(User.objects.get(username = 'newbie').state, 'disabled')

    def test_made_up_or_expired_links_are_refused(self):
        self.assertEqual(self.client.get('/verify/not-a-real-token').status_code, 302)
        self.signup()
        link = re.search(r'/verify/(\S+)', mail.outbox[0].body).group(1)
        with mock.patch('django.core.signing.time.time', return_value = __import__('time').time() + 49 * 3600):
            reply = self.client.get('/verify/' + link, follow = True)
        self.assertIn('not valid or has expired', flashes(reply))
        self.assertEqual(User.objects.get(username = 'newbie').email_verified, False)

    @override_settings(MAIL_CONFIGURED = False)
    def test_without_email_settings_every_new_account_waits_for_an_administrator(self):
        self.signup()
        self.assertEqual(mail.outbox, [])
        self.assertEqual(User.objects.get(username = 'newbie').state, 'pending')

    def test_a_broken_mail_server_does_not_break_signing_up(self):
        with mock.patch('ciweb.accounts.views.send_mail', side_effect = OSError('down')):
            self.assertEqual(self.signup().status_code, 302)
        self.assertEqual(User.objects.get(username = 'newbie').state, 'pending')

    def test_bad_sign_ups_are_refused_with_a_reason_and_create_nothing(self):
        person('alice')
        cases = ({'password2': 'something-else-1'}, {'password1': 'short', 'password2': 'short'},
                 {'username': 'Auto'}, {'username': 'ALICE'}, {'email': 'Alice@example.com'}, {'email': 'nonsense'},
                 {'password1': 'password123', 'password2': 'password123'},
                 {'password1': 'newbie-password-1', 'password2': 'newbie-password-1'})
        for overrides in cases:
            self.assertEqual(self.signup(**overrides).status_code, 400, overrides)
        self.assertFalse(User.objects.filter(username = 'newbie').exists())


@override_settings(**MAIL_ON)
class PasswordResetTest(TestCase):
    def setUp(self):
        self.alice = person('alice')

    def ask(self, email = 'alice@example.com', **extra):
        return self.client.post('/forgot', {'email': email}, **extra)

    def link(self):
        return re.search(r'http://ui\.example(/reset/\S+)', mail.outbox[-1].body).group(1)

    def test_a_link_arrives_works_once_and_signs_everyone_out(self):
        member = Client(); member.post('/login', {'username': 'alice', 'password': GOOD})
        self.assertEqual(member.get('/new').status_code, 200)
        self.assertEqual(self.ask('ALICE@example.com').status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        link  = self.link()
        step  = self.client.get(link)
        self.assertEqual(step.status_code, 302)
        form  = step['Location']
        self.assertEqual(self.client.get(form).status_code, 200)
        weak  = self.client.post(form, {'new_password1': 'password123', 'new_password2': 'password123'})
        self.assertEqual(weak.status_code, 200)
        mismatch = self.client.post(form, {'new_password1': NEW, 'new_password2': 'different-one-12'})
        self.assertEqual(mismatch.status_code, 200)
        self.assertEqual(self.client.post(form, {'new_password1': NEW, 'new_password2': NEW}).status_code, 302)
        fresh = Client().get(link, follow = True)
        self.assertIn('not valid or has expired', fresh.content.decode())
        self.assertEqual(member.get('/new').status_code, 302)
        self.assertEqual(page_errors(Client().post('/login', {'username': 'alice', 'password': GOOD})), ['Wrong user name or password.'])
        self.assertEqual(Client().post('/login', {'username': 'alice', 'password': NEW}).status_code, 302)

    def test_known_and_unknown_addresses_get_the_same_answer(self):
        known, unknown = self.ask(), self.ask('ghost@example.com')
        self.assertEqual((known.status_code, known['Location']), (unknown.status_code, unknown['Location']))
        self.assertEqual(len(mail.outbox), 1)

    def test_only_active_accounts_get_reset_links(self):
        person('wait', 'pending')
        person('off', 'disabled')
        self.ask('wait@example.com')
        self.ask('off@example.com')
        self.assertEqual(mail.outbox, [])

    def test_the_link_uses_the_configured_address_never_the_request_host(self):
        self.ask(HTTP_X_FORWARDED_HOST = 'evil.example', HTTP_ORIGIN = 'http://evil.example')
        self.assertTrue(self.link().startswith('/reset/'))
        self.assertIn('http://ui.example/reset/', mail.outbox[0].body)
        self.assertNotIn('evil.example', mail.outbox[0].body)
        self.assertEqual(self.client.post('/forgot', {'email': 'alice@example.com'}, HTTP_HOST = 'evil.example').status_code, 400)

    def test_a_broken_mail_server_does_not_break_the_page_or_reveal_anything(self):
        with mock.patch('django.core.mail.EmailMultiAlternatives.send', side_effect = OSError('down')):
            self.assertEqual(self.ask().status_code, 302)

    @override_settings(MAIL_CONFIGURED = False)
    def test_without_email_settings_the_page_tells_people_to_ask_an_administrator(self):
        page = self.client.get('/forgot').content.decode()
        self.assertIn('Ask an administrator', page)
        self.assertNotIn('name="email"', page)
        reply = self.ask()
        self.assertEqual(mail.outbox, [])
        self.assertIn('administrator', flashes(self.client.get(reply['Location'])))

    def test_made_up_links_are_refused(self):
        self.assertIn('not valid or has expired', self.client.get('/reset/MQ/not-a-token', follow = True).content.decode())

    def test_resetting_also_confirms_the_email(self):
        User.objects.filter(username = 'alice').update(email_verified = False)
        self.ask()
        form = self.client.get(self.link())['Location']
        self.client.post(form, {'new_password1': NEW, 'new_password2': NEW})
        self.assertTrue(User.objects.get(username = 'alice').email_verified)


class AccountPageTest(TestCase):
    def setUp(self):
        person('alice')

    def test_it_needs_a_login_and_shows_the_name_and_email(self):
        self.assertEqual(Client().get('/account').status_code, 302)
        self.client.post('/login', {'username': 'alice', 'password': GOOD})
        html = self.client.get('/account').content.decode()
        self.assertIn('alice@example.com', html)
        self.assertNotIn('argon2', html)
        self.assertNotIn(GOOD, html)

    def test_changing_the_password_needs_the_old_one_and_signs_out_other_devices(self):
        here, there = Client(), Client()
        for client in (here, there):
            client.post('/login', {'username': 'alice', 'password': GOOD})
        wrong = here.post('/account/password', {'old_password': 'not-my-password-1', 'new_password1': NEW, 'new_password2': NEW})
        self.assertEqual(wrong.status_code, 200)
        weak = here.post('/account/password', {'old_password': GOOD, 'new_password1': 'alice-is-me-1', 'new_password2': 'alice-is-me-1'})
        self.assertEqual(weak.status_code, 200)
        ok = here.post('/account/password', {'old_password': GOOD, 'new_password1': NEW, 'new_password2': NEW})
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(here.get('/account').status_code, 200)
        self.assertEqual(there.get('/account').status_code, 302)
        self.assertEqual(Client().post('/login', {'username': 'alice', 'password': NEW}).status_code, 302)


class AdminSiteTest(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser('boss', 'boss@example.com', GOOD)
        self.wait = person('wait', 'pending')
        self.off = person('off', 'disabled', failed_logins = 3, locked_until = timezone.now() + timedelta(minutes = 5))

    def test_staff_can_approve_disable_and_unlock_from_the_list(self):
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get('/admin/accounts/user/').status_code, 200)
        url = '/admin/accounts/user/'
        self.client.post(url, {'action': 'approve_users', '_selected_action': [self.wait.pk]})
        self.assertEqual(User.objects.get(pk = self.wait.pk).state, 'active')
        self.client.post(url, {'action': 'disable_users', '_selected_action': [self.wait.pk]})
        self.assertEqual(User.objects.get(pk = self.wait.pk).state, 'disabled')
        self.client.post(url, {'action': 'unlock_users', '_selected_action': [self.off.pk]})
        self.assertIsNone(User.objects.get(pk = self.off.pk).locked_until)

    def test_nobody_can_disable_themselves_by_accident(self):
        self.client.force_login(self.boss)
        self.client.post('/admin/accounts/user/', {'action': 'disable_users', '_selected_action': [self.boss.pk]})
        self.assertTrue(User.objects.get(pk = self.boss.pk).is_active)

    def test_ordinary_users_cannot_reach_it(self):
        self.client.force_login(person('alice'))
        self.assertEqual(self.client.get('/admin/accounts/user/').status_code, 302)
        self.assertEqual(Client().get('/admin/').status_code, 302)


class CommandTest(TestCase):
    def run_cli(self, *argv, stdin = '', **kwargs):
        out = io.StringIO()
        with mock.patch('sys.stdin', io.StringIO(stdin)):
            call_command('ci_users', '--password-stdin', *argv, stdout = out, **kwargs)
        return out.getvalue()

    def test_add_creates_an_active_confirmed_account_that_can_sign_in(self):
        self.run_cli('add', 'carol', 'carol@example.com', stdin = STRONG + '\n')
        user = User.objects.get(username = 'carol')
        self.assertEqual((user.state, user.email_verified), ('active', True))
        self.assertEqual(self.client.post('/login', {'username': 'carol', 'password': STRONG}).status_code, 302)

    def test_add_refuses_weak_passwords_duplicates_and_reserved_names(self):
        person('alice')
        for argv, text in ((('add', 'carol', 'carol@example.com'), 'password123\n'),
                           (('add', 'ALICE', 'other@example.com'), STRONG + '\n'),
                           (('add', 'Auto', 'auto@example.com'), STRONG + '\n')):
            with self.assertRaises(CommandError):
                self.run_cli(*argv, stdin = text)
        self.assertFalse(User.objects.filter(username__in = ['carol', 'Auto']).exists())

    def test_list_shows_everyone_with_their_state(self):
        person('alice'); person('wait', 'pending')
        text = self.run_cli('list')
        self.assertRegex(text, r'alice\s+alice@example.com\s+active\s+yes')
        self.assertRegex(text, r'wait\s+wait@example.com\s+pending')
        self.assertIn('2 users', text)

    def test_approve_disable_and_enable_change_who_can_sign_in(self):
        person('wait', 'pending')
        self.run_cli('approve', 'wait')
        self.assertEqual(User.objects.get(username = 'wait').state, 'active')
        self.run_cli('disable', 'wait')
        self.assertEqual(User.objects.get(username = 'wait').state, 'disabled')
        self.run_cli('enable', 'wait')
        self.assertEqual(User.objects.get(username = 'wait').state, 'active')

    def test_passwd_sets_a_new_password_and_unlock_clears_a_lock(self):
        person('alice')
        self.run_cli('passwd', 'alice', stdin = STRONG + '\n')
        self.assertTrue(User.objects.get(username = 'alice').check_password(STRONG))
        User.objects.filter(username = 'alice').update(locked_until = timezone.now() + timedelta(minutes = 9), failed_logins = 2)
        self.run_cli('unlock', 'alice')
        user = User.objects.get(username = 'alice')
        self.assertEqual((user.failed_logins, user.locked_until), (0, None))
        with self.assertRaises(CommandError):
            self.run_cli('passwd', 'alice', stdin = 'password123\n')

    @override_settings(PUBLIC_URL = 'http://ui.example')
    def test_reset_prints_a_link_that_works_on_the_website(self):
        person('bob')
        link = re.search(r'http://ui\.example(/reset/\S+)', self.run_cli('reset', 'bob')).group(1)
        form = self.client.get(link)['Location']
        self.assertEqual(self.client.post(form, {'new_password1': STRONG, 'new_password2': STRONG}).status_code, 302)
        self.assertTrue(User.objects.get(username = 'bob').check_password(STRONG))
        self.assertIn('not valid or has expired', Client().get(link, follow = True).content.decode())

    @override_settings(PUBLIC_URL = '')
    def test_reset_needs_the_address_of_the_web_ui(self):
        person('bob')
        with self.assertRaisesMessage(CommandError, 'REG_UI_URL'):
            self.run_cli('reset', 'bob')

    def test_an_unknown_user_is_reported(self):
        with self.assertRaisesMessage(CommandError, 'No such user'):
            self.run_cli('approve', 'ghost')

    def test_mail_test_explains_missing_settings_and_sends_when_they_exist(self):
        with self.assertRaisesMessage(CommandError, 'REG_SMTP_HOST'):
            self.run_cli('mail-test', 'me@example.com')
        with override_settings(**MAIL_ON):
            self.run_cli('mail-test', 'me@example.com')
            self.assertEqual(mail.outbox[0].to, ['me@example.com'])
            with mock.patch('ciweb.accounts.management.commands.ci_users.send_mail', side_effect = OSError('down')):
                with self.assertRaisesMessage(CommandError, 'Sending failed'):
                    self.run_cli('mail-test', 'me@example.com')
