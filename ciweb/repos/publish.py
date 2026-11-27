"""Writing the repo profiles for the Buildbot master and the runners (see hub/repos.py)."""
from django.conf import settings

from hub import repos as hub_repos
from ciweb.repos.models import RepoProfile


def configs():
    return [profile.to_config() for profile in RepoProfile.objects.prefetch_related('branches')]


def publish():
    """Write the snapshot. Returns True when it changed (the master then reloads it within seconds)."""
    path = settings.PROFILES_FILE
    if not path:
        return False
    return hub_repos.save_snapshot(path, configs())


def sync_state():
    """How far the master is: ('applied' | 'pending' | 'error' | 'unknown', text)."""
    path = settings.PROFILES_FILE
    if not path:
        return 'unknown', 'Profiles are not written to a file (CI_PROFILES_FILE and CI_RESULTS_DIR are not set).'
    applied = hub_repos.read_applied(path)
    if not applied:
        return 'unknown', 'The Buildbot master has not reported yet. It reports when it starts; check that it is running.'
    current = hub_repos.text_version(hub_repos.read_snapshot_text(path))
    if applied.get('version') == current:
        return 'applied', 'Buildbot is using these settings.'
    if applied.get('error'):
        return 'error', applied['error']
    return 'pending', 'Saved. Buildbot loads the change within a few seconds, without stopping running builds.'
