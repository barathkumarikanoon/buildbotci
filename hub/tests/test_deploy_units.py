"""The systemd unit templates in deploy/systemd/ keep the properties that make them safe to run in production."""
import os
import re
import unittest

HERE  = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'deploy', 'systemd')
DB    = 'postgresql@localdbs.service'


def unit(name):
    with open(os.path.join(HERE, name), encoding = 'utf8') as handle:
        return handle.read()


def setting(text, key):
    return [m.group(1).strip() for m in re.finditer(r'^%s=(.*)$' % key, text, re.M)]


class UnitTest(unittest.TestCase):
    def test_master_and_web_ui_need_the_database_unit_and_wait_for_logins(self):
        for name in ('buildbot-master.service', 'ci-ui.service'):
            text = unit(name)
            self.assertIn(DB, ' '.join(setting(text, 'Requires')), name)
            self.assertIn(DB, ' '.join(setting(text, 'After')), name)
            self.assertTrue(any('bin/wait-for-db' in line for line in setting(text, 'ExecStartPre')), name)
            self.assertNotIn('postgresql.service', ' '.join(setting(text, 'After')), name)

    def test_workers_follow_the_master_and_do_not_need_the_database(self):
        text = unit('buildbot-worker@.service')
        self.assertNotIn(DB, ' '.join(setting(text, 'Requires') + setting(text, 'Wants') + setting(text, 'After')))
        self.assertIn('buildbot-master.service', ' '.join(setting(text, 'After')))

    def test_every_unit_has_limits_and_hardening(self):
        for name in ('buildbot-master.service', 'ci-ui.service', 'buildbot-worker@.service'):
            text = unit(name)
            for key in ('MemoryMax', 'TimeoutStopSec', 'NoNewPrivileges', 'PrivateTmp', 'StartLimitBurst', 'Restart'):
                self.assertTrue(setting(text, key), '%s lacks %s' % (name, key))
            self.assertEqual(setting(text, 'NoNewPrivileges'), ['yes'], name)

    def test_the_database_unit_itself_is_not_part_of_this_repository(self):
        self.assertFalse(os.path.exists(os.path.join(HERE, 'postgresql@.service')))
        self.assertFalse([n for n in os.listdir(HERE) if n.startswith('postgresql')])


if __name__ == '__main__':
    unittest.main()
