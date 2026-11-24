from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from ciweb.db import database_from_url


class DatabaseUrlTest(SimpleTestCase):
    def test_postgres_url_parts(self):
        config = database_from_url('postgresql+psycopg2://user:p%40ss@db.example:6543/buildbot')
        self.assertEqual((config['NAME'], config['USER'], config['PASSWORD'], config['HOST'], config['PORT']),
                         ('buildbot', 'user', 'p@ss', 'db.example', '6543'))
        self.assertNotIn('OPTIONS', config)

    def test_the_schema_becomes_the_search_path(self):
        config = database_from_url('postgresql://u:p@h/db', 'ci')
        self.assertEqual(config['OPTIONS'], {'options': '-c search_path=ci'})

    def test_tls_and_timeout_options_in_the_url_reach_the_connection(self):
        config = database_from_url('postgresql://u:p@h/db?sslmode=verify-full&sslrootcert=/etc/ca.pem&connect_timeout=5', 'ci')
        self.assertEqual(config['OPTIONS'], {'sslmode': 'verify-full', 'sslrootcert': '/etc/ca.pem',
                                             'connect_timeout': '5', 'options': '-c search_path=ci'})

    def test_unknown_url_options_are_ignored(self):
        config = database_from_url('postgresql://u:p@h/db?sslmode=require&evil=1&options=-c%20x')
        self.assertEqual(config['OPTIONS'], {'sslmode': 'require'})

    def test_sqlite_and_unknown_schemes(self):
        self.assertEqual(database_from_url('sqlite://')['NAME'], ':memory:')
        self.assertIsNone(database_from_url(''))
        with self.assertRaises(ImproperlyConfigured):
            database_from_url('mysql://u:p@h/db')
