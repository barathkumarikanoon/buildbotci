import os

from aiohttp import web
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ciweb.gateway import PREFIX, Gateway


class Command(BaseCommand):
    help = 'Serves the web UI at / and Buildbot at /buildbot/ on one address.'

    def add_arguments(self, parser):
        parser.add_argument('addrport', nargs = '?', default = os.environ.get('REG_GATEWAY_BIND', '127.0.0.1:8080'),
                            help = 'address to listen on, default 127.0.0.1:8080')
        parser.add_argument('--ui', default = os.environ.get('REG_GATEWAY_UI', 'http://127.0.0.1:8020'),
                            help = 'where the web UI runs, default http://127.0.0.1:8020')
        parser.add_argument('--buildbot', default = settings.BUILDBOT_URL or 'http://localhost:8010/',
                            help = 'where Buildbot runs (REG_BUILDBOT_URL), default http://localhost:8010/')

    def handle(self, *args, **options):
        host, _, port = options['addrport'].rpartition(':')
        if not port.isdigit():
            raise CommandError('Give the address as host:port, for example 127.0.0.1:8080')
        gateway = Gateway(options['ui'], options['buildbot'])
        self.stdout.write('One address: http://%s:%s/ -> web UI %s, %s/ -> Buildbot %s' % (
            host or '127.0.0.1', port, options['ui'], PREFIX, options['buildbot']))
        web.run_app(gateway.application(), host = host or '127.0.0.1', port = int(port), print = None)
