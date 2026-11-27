import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings

from ciweb.runs.tests.test_views import make_repo
from ciweb.accounts.tests.test_accounts import GOOD, MAIL_ON, STRONG, person

User = get_user_model()


def boss(name = 'boss', **extra):
    return person(name, is_staff = True, **extra)


class HeaderTest(TestCase):
    def setUp(self):
        make_repo()

    def test_anonymous_visitors_see_sign_in_and_create_account(self):
        html = self.client.get('/').content.decode()
        self.assertIn('Sign in', html)
        self.assertIn('Create account', html)
        self.assertNotIn('Sign out', html)

    def test_signed_in_people_see_who_they_are_and_a_clear_sign_out_button(self):
        self.client.force_login(person('alice'))
        for path in ('/', '/r/indianlaw/new', '/account'):
            html = self.client.get(path).content.decode()
            self.assertIn('class="signout"', html, path)
            self.assertIn('Sign out', html, path)
            self.assertIn('class="me"', html, path)
            self.assertIn('>A</span>', html, path)
            self.assertNotIn('People', html, path)

    def test_the_sign_out_button_really_signs_you_out(self):
        self.client.force_login(person('alice'))
        html = self.client.get('/').content.decode()
        action = re.search(r'<form method="post" action="([^"]+)" class="inline">', html).group(1)
        self.assertEqual(self.client.post(action).status_code, 302)
        self.assertNotIn('Sign out', self.client.get('/').content.decode())

    def test_administrators_see_a_people_link_with_the_number_waiting(self):
        self.client.force_login(boss())
        html = self.client.get('/').content.decode()
        self.assertIn('People', html)
        self.assertNotIn('class="count"', html)
        self.assertNotIn('class="banner"', html)
        person('wait1', 'pending'); person('wait2', 'pending')
        html = self.client.get('/').content.decode()
        self.assertIn('<span class="count">2</span>', html)
        self.assertIn('<strong>2 sign-ups</strong> waiting for your approval', html)

    def test_ordinary_users_never_see_the_queue(self):
        person('wait1', 'pending')
        self.client.force_login(person('alice'))
        html = self.client.get('/').content.decode()
        self.assertNotIn('waiting for your approval', html)
        self.assertNotIn('class="count"', html)


class PeoplePageTest(TestCase):
    def setUp(self):
        make_repo()
        self.boss = boss()
        self.client.force_login(self.boss)

    def act(self, user, action, client = None):
        return (client or self.client).post('/people/%d/%s' % (user.pk, action))

    def test_only_administrators_can_open_it(self):
        self.assertEqual(self.client.get('/people').status_code, 200)
        stranger = Client()
        reply = stranger.get('/people')
        self.assertEqual(reply.status_code, 302)
        self.assertIn('/login', reply['Location'])
        member = Client(); member.force_login(person('alice'))
        self.assertEqual(member.get('/people').status_code, 403)
        waiting = person('wait', 'pending')
        self.assertEqual(self.act(waiting, 'approve', member).status_code, 403)
        self.assertEqual(self.act(waiting, 'approve', stranger).status_code, 302)
        self.assertEqual(User.objects.get(pk = waiting.pk).state, 'pending')

    def test_it_lists_who_is_waiting_first_and_everyone_else_below(self):
        person('wait', 'pending', email_verified = False)
        person('off', 'disabled')
        html = self.client.get('/people').content.decode()
        self.assertLess(html.index('Waiting for approval'), html.index('Everyone else'))
        queue, rest = html.split('Everyone else')
        self.assertIn('wait', queue)
        self.assertIn('wait@example.com', queue)
        self.assertIn('not yet', queue)
        self.assertNotIn('boss@example.com', queue)
        self.assertIn('off', rest)
        self.assertIn('administrator', rest)
        self.assertIn('(you)', rest)

    def test_an_empty_queue_says_so(self):
        self.assertIn('Nobody is waiting', self.client.get('/people').content.decode())

    def test_approving_lets_the_person_in(self):
        waiting = person('wait', 'pending')
        self.assertEqual(Client().post('/login', {'username': 'wait', 'password': GOOD}).status_code, 200)
        reply = self.act(waiting, 'approve')
        self.assertEqual(reply.status_code, 302)
        self.assertEqual(User.objects.get(pk = waiting.pk).state, 'active')
        self.assertEqual(Client().post('/login', {'username': 'wait', 'password': GOOD}).status_code, 302)
        self.assertIn('is approved', ' '.join(m.message for m in self.client.get('/people').context['messages']))

    def test_rejecting_removes_the_sign_up_so_they_can_try_again(self):
        waiting = person('wait', 'pending')
        self.act(waiting, 'reject')
        self.assertFalse(User.objects.filter(pk = waiting.pk).exists())

    def test_only_waiting_sign_ups_can_be_rejected(self):
        active = person('alice')
        self.act(active, 'reject')
        self.assertTrue(User.objects.filter(pk = active.pk).exists())

    def test_disabling_signs_the_person_out_and_enabling_brings_them_back(self):
        alice = person('alice')
        member = Client(); member.force_login(alice)
        self.assertEqual(member.get('/r/indianlaw/new').status_code, 200)
        self.act(alice, 'disable')
        self.assertEqual(User.objects.get(pk = alice.pk).state, 'disabled')
        self.assertEqual(member.get('/r/indianlaw/new').status_code, 302)
        self.act(alice, 'approve')
        self.assertEqual(User.objects.get(pk = alice.pk).state, 'active')

    def test_nobody_can_disable_themselves(self):
        self.act(self.boss, 'disable')
        self.assertTrue(User.objects.get(pk = self.boss.pk).is_active)

    def test_unlock_clears_a_lock(self):
        alice = person('alice')
        for _ in range(5):
            Client().post('/login', {'username': 'alice', 'password': 'wrong-password-1'})
        self.assertIn('locked', self.client.get('/people').content.decode())
        self.act(alice, 'unlock')
        self.assertEqual(Client().post('/login', {'username': 'alice', 'password': GOOD}).status_code, 302)

    def test_actions_need_a_post_and_a_valid_form_token_and_a_known_action(self):
        waiting = person('wait', 'pending')
        self.assertEqual(self.client.get('/people/%d/approve' % waiting.pk).status_code, 405)
        strict = Client(enforce_csrf_checks = True); strict.force_login(self.boss)
        self.assertEqual(strict.post('/people/%d/approve' % waiting.pk).status_code, 403)
        self.act(waiting, 'make-me-king')
        self.assertEqual(User.objects.get(pk = waiting.pk).state, 'pending')
        self.assertEqual(self.client.post('/people/9999/approve').status_code, 302)


@override_settings(**MAIL_ON, SIGNUP_DOMAINS = ())
class NotificationTest(TestCase):
    def signup(self):
        return Client().post('/signup', {'username': 'newbie', 'email': 'newbie@example.com', 'password1': STRONG, 'password2': STRONG})

    def test_administrators_are_emailed_when_someone_signs_up(self):
        boss('boss'); boss('chief')
        person('alice')
        self.signup()
        to_admins = [m for m in mail.outbox if m.subject.startswith('New sign-up waiting')]
        self.assertEqual(len(to_admins), 1)
        self.assertEqual(sorted(to_admins[0].to), ['boss@example.com', 'chief@example.com'])
        self.assertIn('newbie', to_admins[0].body)
        self.assertIn('newbie@example.com', to_admins[0].body)
        self.assertIn('http://ui.example/people', to_admins[0].body)

    def test_disabled_administrators_and_ordinary_users_are_not_emailed(self):
        person('boss', 'disabled', is_staff = True)
        person('alice')
        self.signup()
        self.assertEqual([m for m in mail.outbox if m.subject.startswith('New sign-up')], [])

    @override_settings(MAIL_CONFIGURED = False)
    def test_without_email_settings_the_queue_still_works(self):
        boss()
        self.signup()
        self.assertEqual(mail.outbox, [])
        client = Client(); client.force_login(User.objects.get(username = 'boss'))
        self.assertIn('1 sign-up</strong> waiting', client.get('/').content.decode())

    def test_a_broken_mail_server_does_not_stop_the_sign_up(self):
        boss()
        with mock.patch('ciweb.accounts.notify.send_mail', side_effect = OSError('down')):
            self.assertEqual(self.signup().status_code, 302)
        self.assertEqual(User.objects.get(username = 'newbie').state, 'pending')

    def test_the_person_is_told_when_approved_or_rejected(self):
        admin = Client(); admin.force_login(boss())
        self.signup()
        newbie = User.objects.get(username = 'newbie')
        mail.outbox.clear()
        admin.post('/people/%d/approve' % newbie.pk)
        self.assertEqual([m.to for m in mail.outbox], [['newbie@example.com']])
        self.assertIn('approved', mail.outbox[0].subject)
        self.assertIn('http://ui.example/login', mail.outbox[0].body)
        other = person('other', 'pending')
        mail.outbox.clear()
        admin.post('/people/%d/reject' % other.pk)
        self.assertIn('not approved', mail.outbox[0].subject)
