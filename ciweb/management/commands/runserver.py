import ipaddress

from django.conf import settings
from django.contrib.staticfiles.management.commands.runserver import Command as StaticRunserver
from django.core.management.base import CommandError

from ciweb import envfile


def is_loopback(addr):
    if addr in ('localhost', ''):
        return True
    try:
        return ipaddress.ip_address(addr.strip('[]')).is_loopback
    except ValueError:
        return False


class Command(StaticRunserver):
    default_port = '8020'
    help = "Starts Django's development server on 127.0.0.1:8020 with automatic reloading of code, templates and styles."

    def handle(self, *args, **options):
        addr = options.get('addrport') or ''
        host = addr.rsplit(':', 1)[0] if ':' in addr else (addr if not addr.isdigit() else '')
        if settings.DEBUG and not is_loopback(host):
            raise CommandError('Debug pages show settings and code, so they must not be exposed on %s. '
                               'Use 127.0.0.1, or set REG_UI_DEBUG=0.' % host)
        return super().handle(*args, **options)

    def inner_run(self, *args, **options):
        if envfile.CONFLICTS:
            names = sorted(envfile.CONFLICTS)
            self.stdout.write(self.style.WARNING(
                'These settings come from your terminal and differ from the settings file: %s. '
                'Run "unset %s" (or open a new terminal) to use the file.' % (', '.join(names), ' '.join(names))))
        self.stdout.write('Branches offered: %s' % ', '.join(settings.BRANCHES))
        return super().inner_run(*args, **options)
