import os

from django.core.management.commands.migrate import Command as MigrateCommand
from django.db import connection


class Command(MigrateCommand):
    def handle(self, *args, **options):
        schema = os.environ.get('REG_UI_DB_SCHEMA', '')
        if schema and connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('CREATE SCHEMA IF NOT EXISTS "%s"' % schema.replace('"', ''))
        return super().handle(*args, **options)
