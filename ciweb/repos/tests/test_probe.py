import os
import subprocess
import tempfile
import unittest
import unittest.mock

from django.test import TestCase, override_settings

from ciweb.repos import probe
from ciweb.repos.tests.test_settings import SettingsTestBase


def git(path, *args):
    subprocess.run(['git', '-C', path] + list(args), check = True, stdout = subprocess.PIPE, stderr = subprocess.PIPE,
                   env = dict(os.environ, GIT_AUTHOR_NAME = 't', GIT_AUTHOR_EMAIL = 't@e.x', GIT_COMMITTER_NAME = 't', GIT_COMMITTER_EMAIL = 't@e.x'))


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'Indian-Law')
        os.makedirs(self.path)
        git(self.path, 'init', '-q')
        git(self.path, 'commit', '-q', '--allow-empty', '-m', 'first')
        git(self.path, 'remote', 'add', 'origin', 'https://user:s3cret@example.com/org/indianlaw.git')
        for branch in ('main', '2026', 'release/1.0'):
            git(self.path, 'update-ref', 'refs/remotes/origin/' + branch, 'HEAD')
        git(self.path, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')

    def test_it_reads_the_clone(self):
        found = probe.probe(self.path)
        self.assertTrue(found['folder']['ok'])
        self.assertTrue(found['git']['ok'])
        self.assertEqual(found['branches'], ['2026', 'main', 'release/1.0'])
        self.assertEqual(found['default_branch'], 'main')

    def test_a_password_in_the_address_is_never_shown(self):
        found = probe.probe(self.path)
        self.assertNotIn('s3cret', str(found))
        self.assertIn('example.com/org/indianlaw.git', found['git']['origin'])

    def test_it_suggests_the_rest_from_the_folder(self):
        suggest = probe.probe(self.path)['suggest']
        self.assertEqual((suggest['key'], suggest['name'], suggest['checkout_name']), ('indian-law', 'Indian Law', 'Indian-Law'))

    def test_the_test_suite_type_is_detected(self):
        self.assertEqual(probe.probe(self.path)['suggest']['adapter'], 'command')
        os.makedirs(os.path.join(self.path, 'tests'))
        open(os.path.join(self.path, 'tests', 'test_a.py'), 'w').close()
        self.assertEqual(probe.probe(self.path)['suggest']['adapter'], 'pytest')
        os.makedirs(os.path.join(self.path, 'regressions'))
        open(os.path.join(self.path, 'regressions', 'regression.py'), 'w').close()
        found = probe.probe(self.path)['suggest']
        self.assertEqual(found['adapter'], 'comparison')
        self.assertIn('regression.py', found['adapter_why'])

    def test_a_missing_folder_and_a_non_repository_say_so(self):
        self.assertFalse(probe.probe(os.path.join(self.tmp.name, 'nope'))['folder']['ok'])
        plain = os.path.join(self.tmp.name, 'plain')
        os.makedirs(plain)
        found = probe.probe(plain)
        self.assertTrue(found['folder']['ok'])
        self.assertFalse(found['git']['ok'])
        self.assertEqual(probe.probe('')['folder']['text'], 'Type the folder of the git clone.')

    def test_a_clone_without_origin_is_a_problem(self):
        git(self.path, 'remote', 'remove', 'origin')
        self.assertFalse(probe.probe(self.path)['git']['ok'])

    def test_home_is_expanded_and_a_virtualenv_is_suggested(self):
        env = os.path.join(self.tmp.name, 'home', '.indian-law', 'bin')
        os.makedirs(env)
        python = os.path.join(env, 'python')
        open(python, 'w').close()
        os.chmod(python, 0o755)
        with unittest.mock.patch.dict(os.environ, {'HOME': os.path.join(self.tmp.name, 'home')}):
            self.assertEqual(probe.probe(self.path)['suggest']['python'], '$HOME/.indian-law/bin/python')
            self.assertTrue(probe.probe(self.path, '$HOME/.indian-law/bin/python')['python']['ok'])
            self.assertFalse(probe.probe(self.path, '$HOME/.nothing/bin/python')['python']['ok'])

    def test_no_program_of_the_repository_is_run(self):
        marker = os.path.join(self.tmp.name, 'ran')
        script = os.path.join(self.path, 'run.sh')
        with open(script, 'w') as handle:
            handle.write('#!/bin/sh\ntouch %s\n' % marker)
        os.chmod(script, 0o755)
        probe.probe(self.path, script)
        self.assertFalse(os.path.exists(marker))


class ProbeViewTest(SettingsTestBase):
    def test_only_administrators_may_ask(self):
        from ciweb.runs.tests.test_views import make_user
        user = self.client_class()
        user.force_login(make_user('plain'))
        self.assertEqual(user.post('/settings/repos/probe', {'repo_dir': '/tmp'}).status_code, 403)
        self.assertEqual(self.client_class().post('/settings/repos/probe', {'repo_dir': '/tmp'}).status_code, 302)

    def test_it_answers_with_json(self):
        reply = self.admin.post('/settings/repos/probe', {'repo_dir': '/nonexistent/folder', 'python': 'python3'})
        self.assertEqual(reply.status_code, 200)
        data = reply.json()
        self.assertFalse(data['folder']['ok'])
        self.assertIn('python', data)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.admin.get('/settings/repos/probe').status_code, 405)
