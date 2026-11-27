from ciweb.repos.models import Branch, RepoProfile
from ciweb.repos.tests.test_settings import SettingsTestBase, post_data
from ciweb.runs.tests.test_views import make_repo, make_user
from hub import repos as hub_repos


class QuickAddTest(SettingsTestBase):
    def setUp(self):
        super().setUp()
        self.profile = make_repo('egazette', branches = ('master',))

    def add(self, **post):
        post.setdefault('name', 'release/1.0')
        return self.admin.post('/settings/repos/egazette/branches/add', post)

    def test_an_administrator_adds_a_branch_from_the_repo_page(self):
        reply = self.add(scheduled = 'on', cron_hour = '3', cron_minute = '30')
        self.assertEqual(reply.status_code, 302)
        branch = Branch.objects.get(name = 'release/1.0')
        self.assertEqual((branch.scheduled, branch.cron_hour, branch.cron_minute), (True, '3', '30'))
        config = self.snapshot_repos()['egazette']
        self.assertEqual(config.branch_cron('release/1.0'), ('3', '30'))
        self.assertEqual(config.force_scheduler_for('release/1.0'), 'force-egazette-release_1_0')
        self.assertIn('Added the branch release/1.0', self.admin.get(reply['Location']).content.decode())

    def test_the_branch_can_be_for_requests_only(self):
        self.add(name = 'dev')
        self.assertFalse(Branch.objects.get(name = 'dev').scheduled)
        self.assertNotIn('dev', self.snapshot_repos()['egazette'].scheduled_branches)

    def test_a_bad_name_a_bad_time_or_a_duplicate_adds_nothing(self):
        for post in ({'name': 'bad name!'}, {'name': 'ok', 'cron_hour': '99'}, {'name': 'master'}, {'name': 'a..b'}):
            reply = self.add(**post)
            self.assertEqual(reply.status_code, 302)
        self.assertEqual(Branch.objects.count(), 1)

    def test_only_administrators_can_add_a_branch(self):
        user = self.client_class()
        user.force_login(make_user('plain'))
        self.assertEqual(user.post('/settings/repos/egazette/branches/add', {'name': 'x'}).status_code, 403)
        self.assertEqual(Branch.objects.count(), 1)

    def test_only_administrators_see_the_add_button(self):
        page = '/r/egazette/'
        self.assertIn('id="add-branch-dialog"', self.admin.get(page).content.decode())
        user = self.client_class()
        user.force_login(make_user('plain'))
        self.assertNotIn('add-branch-dialog', user.get(page).content.decode())


class BranchFormTest(SettingsTestBase):
    def test_each_branch_keeps_its_own_time(self):
        self.admin.post('/settings/repos/new', post_data(**{
            'branch-0-cron_hour': '', 'branch-0-cron_minute': '',
            'branch-1-cron_hour': '5', 'branch-1-cron_minute': '*/20', 'branch-1-scheduled': 'on'}))
        config = self.snapshot_repos()['egazette']
        self.assertEqual(config.branch_cron('master'), ('3', '15'))          # the repo's own time
        self.assertEqual(config.branch_cron('dev'), ('5', '*/20'))
        self.assertEqual(sorted(config.scheduled_branches), ['dev', 'master'])

    def test_a_bad_time_is_reported_on_the_form_and_nothing_is_saved(self):
        reply = self.admin.post('/settings/repos/new', post_data(**{'branch-1-cron_hour': '31'}))
        self.assertEqual(reply.status_code, 200)
        self.assertFalse(RepoProfile.objects.exists())

    def test_the_form_has_an_add_branch_button_and_a_template_row(self):
        html = self.admin.get('/settings/repos/new').content.decode()
        self.assertIn('id="add-branch"', html)
        self.assertIn('id="branch-template"', html)
        self.assertIn('__prefix__', html)
        self.assertIn('Every night', html)


class WarningDialogsTest(SettingsTestBase):
    def test_disabling_a_repo_warns_first(self):
        make_repo('egazette', name = 'eGazette')
        html = self.admin.get('/settings/repos').content.decode()
        self.assertIn('data-confirm-title="Disable eGazette?"', html)
        self.assertIn('data-confirm-ok="Yes, disable"', html)

    def test_every_page_has_the_shared_warning_dialog(self):
        html = self.admin.get('/settings/repos').content.decode()
        self.assertIn('id="confirm-dialog"', html)
        self.assertIn('runs/app.js', html)


class BrandTest(SettingsTestBase):
    def test_every_page_shows_the_ci_name_even_the_sign_in_page(self):
        from django.test import Client
        for path in ('/', '/login', '/signup'):
            html = Client().get(path).content.decode()
            self.assertIn('<span class="brand-name">Indian Kanoon CI</span>', html, path)
            self.assertNotIn('Continuous Integration', html, path)
            self.assertNotIn('>IK<', html, path)

    def test_the_overview_has_context_not_counters(self):
        make_repo('egazette')
        html = self.admin.get('/').content.decode()
        self.assertNotIn('class="stats', html)
        self.assertNotIn('branches passing', html)
        self.assertIn('Nothing has been tested yet', html)
        self.assertIn('Nightly at 21:00', html)


class WritingRulesTest(SettingsTestBase):
    """The site is written in full words, and its style sheet has no fixed pixel sizes."""

    def visible_text(self, html):
        import re
        body = re.sub(r'<script.*?</script>|<style.*?</style>|<svg.*?</svg>', ' ', html, flags = re.S)
        attributes = ' '.join(m.group(2) for m in re.finditer(r'(title|placeholder|aria-label|data-confirm[-a-z]*)="([^"]*)"', body))
        return re.sub(r'<[^>]+>', ' ', body) + ' ' + attributes

    def test_no_page_uses_an_abbreviation_in_its_visible_text(self):
        import re
        make_repo('egazette', name = 'eGazette')
        banned = re.compile(r'\b(repos?|Repos?|IK|UI|ids?|diffs?|Diff|config|e\.g\.)\b')
        for path in ('/', '/r/egazette/', '/r/egazette/new', '/settings/repos', '/settings/repos/new', '/settings/repos/egazette',
                     '/settings/repos/egazette/delete', '/people', '/login', '/signup'):
            text = self.visible_text(self.admin.get(path).content.decode())
            found = sorted(set(banned.findall(text)))
            self.assertEqual(found, [], '%s uses %s' % (path, found))

    def test_the_style_sheet_has_no_pixel_sizes(self):
        import re
        from django.contrib.staticfiles import finders
        css = open(finders.find('runs/app.css'), encoding = 'utf8').read()
        self.assertEqual(re.findall(r'[\d.]+\s*px\b', css), [])

    def test_the_text_size_follows_the_reader_and_the_screen(self):
        from django.contrib.staticfiles import finders
        css = open(finders.find('runs/app.css'), encoding = 'utf8').read()
        self.assertIn('font-size: clamp(100%', css)
        for width in ('70rem', '56rem', '40rem'):
            self.assertIn('@media (max-width: %s)' % width, css)

    def test_times_and_sizes_are_written_out(self):
        from ciweb.runs import fmt
        self.assertEqual(fmt.span(319), '5 minutes 19 seconds')
        self.assertEqual(fmt.span(61), '1 minute 1 second')
        self.assertEqual(fmt.size(2048), '2.0 kilobytes')


class ThemeTest(SettingsTestBase):
    def test_every_page_offers_the_four_themes_and_sets_the_chosen_one_before_drawing(self):
        html = self.admin.get('/').content.decode()
        for name in ('paper', 'ocean', 'slate', 'midnight'):
            self.assertIn('data-theme-choice="%s"' % name, html)
        self.assertLess(html.index("localStorage.getItem('ci-theme')"), html.index('runs/app.css'))

    def test_each_theme_defines_every_colour_the_site_uses(self):
        import re
        from django.contrib.staticfiles import finders
        css = open(finders.find('runs/app.css'), encoding = 'utf8').read()
        blocks = {name: re.search(r':root\[data-theme="%s"\] \{(.*?)\n\}' % name, css, re.S).group(1) for name in ('ocean', 'slate', 'midnight')}
        blocks['paper'] = re.search(r':root, :root\[data-theme="paper"\] \{(.*?)\n\}', css, re.S).group(1)
        names = {n: set(re.findall(r'(--[a-z0-9-]+):', b)) for n, b in blocks.items()}
        for name, found in names.items():
            self.assertEqual(sorted(names['paper'] - found), [], '%s lacks colours' % name)
            self.assertEqual(sorted(found - names['paper']), [], '%s has extra colours' % name)

    def test_text_colours_are_readable_in_every_theme(self):
        import re
        from django.contrib.staticfiles import finders
        css = open(finders.find('runs/app.css'), encoding = 'utf8').read()

        def luminance(colour):
            r, g, b = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

        def ratio(a, b):
            la, lb = sorted((luminance(a), luminance(b)), reverse = True)
            return (la + 0.05) / (lb + 0.05)

        patterns = {'paper': r':root, :root\[data-theme="paper"\] \{(.*?)\n\}'}
        for name in ('ocean', 'slate', 'midnight'):
            patterns[name] = r':root\[data-theme="%s"\] \{(.*?)\n\}' % name
        for name, pattern in patterns.items():
            v = dict(re.findall(r'(--[a-z0-9-]+): (#[0-9a-f]{6});', re.search(pattern, css, re.S).group(1)))
            for text, ground in (('--ink', '--bg'), ('--ink', '--panel'), ('--ink-2', '--panel'), ('--muted', '--panel'), ('--muted', '--bg'),
                                 ('--accent', '--panel'), ('--accent-ink', '--accent'), ('--nav-ink', '--nav'), ('--nav-muted', '--nav'),
                                 ('--ok', '--ok-bg'), ('--bad', '--bad-bg'), ('--err', '--err-bg'), ('--warn', '--warn-bg'),
                                 ('--run', '--run-bg'), ('--idle', '--idle-bg'), ('--accent-2', '--accent-soft')):
                self.assertGreaterEqual(ratio(v[text], v[ground]), 4.5, '%s: %s on %s is %.1f' % (name, text, ground, ratio(v[text], v[ground])))


class SuiteTypeRenameTest(SettingsTestBase):
    def test_a_repository_saved_with_the_old_type_name_is_shown_with_the_new_one(self):
        make_repo('egazette', adapter = 'indianlaw')
        html = self.admin.get('/settings/repos').content.decode()
        self.assertIn('Output comparison', html)
        self.assertEqual(self.admin.get('/r/egazette/').status_code, 200)

    def test_the_form_offers_the_three_types_by_their_new_names(self):
        html = self.admin.get('/settings/repos/new').content.decode()
        for label in ('Output comparison', 'pytest (Python tests)', 'Command checks'):
            self.assertIn(label, html)
        self.assertNotIn('Indian Kanoon regressions', html)
        self.assertNotIn('Golden folder', html)
        self.assertNotIn('the golden file', html)


class FormActionsTest(SettingsTestBase):
    def test_the_save_and_cancel_buttons_sit_at_the_end_of_the_form_and_do_not_float(self):
        import re
        from django.contrib.staticfiles import finders
        css = open(finders.find('runs/app.css'), encoding = 'utf8').read()
        for rule in re.findall(r'\.form-actions[^{]*\{[^}]*\}', css):
            self.assertNotIn('sticky', rule)
            self.assertNotIn('fixed', rule)
        html = self.admin.get('/settings/repos/new').content.decode()
        self.assertEqual(html.count('class="form-actions"'), 1)
        self.assertLess(html.index('id="step-branches"'), html.index('class="form-actions"'))
        self.assertGreater(html.index('class="form-actions"'), html.index('How many tests at the same time'))
