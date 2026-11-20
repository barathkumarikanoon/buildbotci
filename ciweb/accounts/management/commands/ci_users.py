import getpass
import smtplib
import sys

from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from ciweb.accounts import lockout

User = get_user_model()


class Command(BaseCommand):
    help = 'Manage the accounts of the regression web UI: list, add, approve, enable, disable, reset, passwd, unlock, mail-test'

    def add_arguments(self, parser):
        parser.add_argument('--password-stdin', action = 'store_true', help = 'read the password from standard input instead of asking')
        sub = parser.add_subparsers(dest = 'action', required = True)
        sub.add_parser('list')
        add = sub.add_parser('add')
        add.add_argument('username')
        add.add_argument('email')
        for name in ('approve', 'enable', 'disable', 'unlock', 'passwd', 'reset'):
            sub.add_parser(name).add_argument('username')
        sub.add_parser('mail-test').add_argument('email')

    def handle(self, *args, **options):
        getattr(self, 'do_' + options['action'].replace('-', '_'))(options)

    def user(self, name):
        user = User.objects.find(name)
        if user is None:
            raise CommandError('No such user: %s' % name)
        return user

    def password(self, options, label = 'Password'):
        if options['password_stdin']:
            return sys.stdin.readline().rstrip('\n')
        first  = getpass.getpass('%s: ' % label)
        second = getpass.getpass('%s again: ' % label)
        if first != second:
            raise CommandError('The two passwords are not the same.')
        return first

    def check(self, password, user):
        try:
            password_validation.validate_password(password, user)
        except ValidationError as exc:
            raise CommandError(' '.join(exc.messages))

    def do_add(self, options):
        user = User(username = options['username'], email = options['email'].strip().lower(),
                    is_active = True, approved = True, email_verified = True)
        try:
            user.full_clean(exclude = ['password'])
        except ValidationError as exc:
            raise CommandError('; '.join(' '.join(v) for v in exc.message_dict.values()))
        password = self.password(options)
        self.check(password, user)
        user.set_password(password)
        user.save()
        self.stdout.write('created %s (id %d), active' % (user.username, user.pk))

    def do_list(self, options):
        users = User.objects.order_by('username')
        self.stdout.write('%-18s %-34s %-9s %-9s %s' % ('USER', 'EMAIL', 'STATE', 'EMAIL OK', 'LAST LOGIN (UTC)'))
        for user in users:
            self.stdout.write('%-18s %-34s %-9s %-9s %s%s' % (
                user.username, user.email, user.state, 'yes' if user.email_verified else 'no',
                user.last_login.strftime('%Y-%m-%d %H:%M') if user.last_login else '-',
                ' LOCKED' if lockout.is_locked(user) else ''))
        self.stdout.write('%d users' % users.count())

    def do_approve(self, options):
        user = self.user(options['username'])
        user.approve()
        self.stdout.write('%s is now active' % user.username)

    do_enable = do_approve

    def do_disable(self, options):
        user = self.user(options['username'])
        user.disable()
        self.stdout.write('%s is now disabled' % user.username)

    def do_unlock(self, options):
        user = self.user(options['username'])
        lockout.clear(user)
        self.stdout.write('%s is unlocked' % user.username)

    def do_passwd(self, options):
        user = self.user(options['username'])
        password = self.password(options, 'New password')
        self.check(password, user)
        user.set_password(password)
        user.save(update_fields = ['password'])
        self.stdout.write('password of %s changed; their other sessions ended' % user.username)

    def do_reset(self, options):
        user = self.user(options['username'])
        if not settings.PUBLIC_URL:
            raise CommandError('Set REG_UI_URL to the address of the web UI, so the link can be printed.')
        path = reverse('password_reset_confirm', args = [urlsafe_base64_encode(force_bytes(user.pk)), default_token_generator.make_token(user)])
        self.stdout.write('Send this link to %s. It works for %d minutes, until the password is changed:' % (user.username, settings.PASSWORD_RESET_TIMEOUT // 60))
        self.stdout.write(settings.PUBLIC_URL.rstrip('/') + path)

    def do_mail_test(self, options):
        if not settings.MAIL_CONFIGURED:
            raise CommandError('Email is not set up. Set REG_SMTP_HOST, REG_SMTP_FROM and REG_UI_URL (and REG_SMTP_USER, REG_SMTP_PASSWORD, REG_SMTP_PORT, REG_SMTP_SECURITY).')
        try:
            send_mail('IK Regressions test message', 'If you can read this, password reset emails will work.\n', None, [options['email']])
        except (OSError, smtplib.SMTPException) as exc:
            raise CommandError('Sending failed: %s' % exc)
        self.stdout.write('sent a test message to %s' % options['email'])
