import getpass
import os
import sys
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ciweb.accounts import loginsession, superuser


class Command(BaseCommand):
    help = ('Ask for the user name and password of a website superuser, once for each terminal; exit with an error unless they are '
            'right. The scripts in bin/ that change something run this first. CI_ADMIN_USER and CI_ADMIN_PASSWORD answer it '
            'without a terminal (cron), with no login kept.')

    def add_arguments(self, parser):
        parser.add_argument('--script', default = '', help = 'the script that asks, for the log')
        parser.add_argument('--action', default = '', help = 'what it is about to do, for the log')
        parser.add_argument('--login', action = 'store_true', help = 'bin/login: ask now and say how long it lasts')
        parser.add_argument('--logout', action = 'store_true', help = 'bin/logout: end the login of this terminal')
        parser.add_argument('--who', action = 'store_true', help = 'say who is logged in in this terminal')

    def handle(self, *args, **options):
        if options['logout']:
            who = loginsession.current(touch = False)
            if loginsession.end():
                self.log(who.username if who else '-', 'logout', 'ended')
                self.stdout.write('Logged out. The next script that changes something asks again.')
            else:
                self.stdout.write('Nobody was logged in in this terminal.')
            return
        if options['who']:
            who = loginsession.current(touch = False)
            self.stdout.write('Logged in as %s, for about %d more minutes if unused (bin/logout ends it).' % (who.username, loginsession.minutes_left())
                              if who else 'Nobody is logged in in this terminal.')
            return
        what  = ' '.join(part for part in (options['script'], options['action']) if part) or 'unknown'
        state = superuser.state()
        if state == superuser.UNREACHABLE:
            raise CommandError('The accounts could not be read (is the database running?), so nobody can be let in. '
                               'The programs behind the scripts (buildbot, buildbot-worker, ...) can still be run directly.')
        if state != superuser.READY:
            sys.stderr.write('No website superuser exists yet, so no login is asked. Create one first when you are ready: '
                             'python manage.py createsuperuser\n')
            self.log('(nobody yet)', what, 'allowed: no superuser exists yet')
            return
        if os.environ.get('CI_ADMIN_USER') and os.environ.get('CI_ADMIN_PASSWORD'):          # a job without a terminal: nothing is kept
            user, problem = superuser.check(os.environ['CI_ADMIN_USER'], os.environ['CI_ADMIN_PASSWORD'])
            if user is None:
                self.log(os.environ['CI_ADMIN_USER'], what, 'denied')
                raise CommandError(problem)
            self.log(user.username, what, 'allowed')
            return
        user = loginsession.current()
        if user is not None:
            if options['login']:
                self.stdout.write('Already logged in as %s, for about %d more minutes if unused.' % (user.username, loginsession.minutes_left()))
            else:
                self.log(user.username, what, 'allowed (login of this terminal)')
            return
        login    = os.environ.get('CI_ADMIN_USER') or self.ask(lambda: input('Superuser name: '), 'CI_ADMIN_USER')
        password = os.environ.get('CI_ADMIN_PASSWORD') or self.ask(lambda: getpass.getpass('Superuser password: '), 'CI_ADMIN_PASSWORD')
        user, problem = superuser.check(login, password)
        if user is None:
            self.log(login, what, 'denied')
            raise CommandError(problem)
        loginsession.start(user)
        self.log(user.username, what, 'allowed')
        if loginsession.minutes():
            sys.stderr.write('Logged in as %s. No need to log in again in this terminal for %d minutes of not using it (bin/logout ends it).\n'
                             % (user.username, loginsession.minutes()))

    def ask(self, prompt, setting):
        if not sys.stdin.isatty():
            raise CommandError('No terminal to ask on. Run it in a terminal, or set CI_ADMIN_USER and CI_ADMIN_PASSWORD (%s is missing).' % setting)
        sys.stderr.write('This changes things, so it needs a website superuser.\n')
        try:
            return prompt().strip()
        except (EOFError, KeyboardInterrupt):
            raise CommandError('Cancelled.')

    def log(self, who, what, result):
        """One line for each use, next to the results: when, who, what, and whether it was allowed."""
        if not settings.RESULTS_DIR:
            return
        try:
            os.makedirs(settings.RESULTS_DIR, exist_ok = True)
            with open(os.path.join(settings.RESULTS_DIR, 'admin-actions.log'), 'a', encoding = 'utf8') as handle:
                handle.write('%s  %s  %s  %s\n' % (datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M:%S%z'), who, what, result))
        except OSError:
            pass
