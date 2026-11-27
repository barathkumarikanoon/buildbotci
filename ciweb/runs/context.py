from django.conf import settings
from django.urls import reverse

from ciweb.runs import activity
from hub.store import RUNNING_STATES, Store


def repo_entry(profile, active, current_branch, live = None):
    """One repo of the sidebar: its latest result, how many runs are in progress, and (for the open repo) its branches."""
    status = ''
    try:
        data = Store(profile.to_config().results_dir(settings.RESULTS_DIR))
        recent, _ = data.list_runs(limit = 8)
        busy = activity.summary(activity.settle(data.list_runs(limit = 20, state = 'running', recent = 100)[0]
                                                + data.list_runs(limit = 20, state = 'queued', recent = 100)[0], live))
    except OSError:
        recent, busy = [], activity.summary([])
    for run in recent:
        if run.get('state') not in RUNNING_STATES:
            status = run.get('result') or run.get('state') or ''
            break
    entry = {'key': profile.key, 'name': profile.name, 'status': status, 'busy': busy['busy'], 'busy_text': busy['text'],
             'running': busy['running'], 'active': active,
             'url': reverse('repo_runs', args = [profile.key])}
    if active:
        entry['branches'] = [{'name': b.name, 'current': b.name == current_branch,
                              'url': '%s?branch=%s' % (reverse('repo_runs', args = [profile.key]), b.name)}
                             for b in profile.branch_list()]
        entry['new_url'] = reverse('new_run', args = [profile.key])
    return entry


def site(request):
    """The name of the site and the repos of the sidebar, on every page."""
    from ciweb.repos.models import RepoProfile
    match  = getattr(request, 'resolver_match', None)
    active = match.kwargs.get('key') if match else None
    branch = request.GET.get('branch') if hasattr(request, 'GET') else None
    try:
        live = activity.buildbot_live()
        repos = [repo_entry(p, p.key == active, branch, live) for p in RepoProfile.objects.filter(enabled = True).prefetch_related('branches')]
    except Exception:       # the table does not exist yet (before the first migrate)
        repos = []
    return {'ci_name': settings.SITE_NAME, 'nav_repos': repos,
            'nav_section': (match.url_name if match else '') or ''}
