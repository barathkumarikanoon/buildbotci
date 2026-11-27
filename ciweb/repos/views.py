import os

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from hub import repos as hub_repos
from ciweb.accounts.views import staff_required
from ciweb.repos import decommission, health, probe, publish, workerctl
from ciweb.runs import buildbot_client
from ciweb.repos.forms import BranchForm, BranchFormSet, RepoProfileForm
from ciweb.repos.models import Branch, RepoProfile


def find(key):
    try:
        return RepoProfile.objects.get(key = key)
    except RepoProfile.DoesNotExist:
        raise Http404()


def worker_states():
    """Which workers are connected to the master, or None when Buildbot cannot be asked (the pages say so)."""
    try:
        return buildbot_client.worker_states(settings.BUILDBOT_URL, settings.BUILDBOT_USER, settings.BUILDBOT_PASS)
    except buildbot_client.BuildbotError:
        return None


@staff_required
def repo_list(request):
    state, text = publish.sync_state()
    states = worker_states()
    rows = []
    for r in RepoProfile.objects.prefetch_related('branches'):
        workers = health.worker_rows(r.to_config(), settings.WORKERS_DIR, states)
        rows.append({'repo': r, 'branches': r.branch_names, 'workers': workers, 'worker_summary': health.worker_summary(workers),
                     'open_runs': len(decommission.open_runs(r.to_config())) if r.enabled else 0})
    return render(request, 'repos/list.html', {'rows': rows, 'sync_state': state, 'sync_text': text})


@staff_required
def repo_edit(request, key = None):
    profile = find(key) if key else RepoProfile()
    creating = not profile.pk
    if request.method == 'POST':
        form    = RepoProfileForm(request.POST, instance = profile)
        formset = BranchFormSet(request.POST, instance = profile, prefix = 'branch')
        if form.is_valid() and formset.is_valid() and form.cross_check(formset.kept()):
            with transaction.atomic():
                saved = form.save()
                formset.instance = saved
                formset.save()
                for position, branch in enumerate(saved.branches.all()):
                    if branch.position != position:
                        branch.position = position
                        branch.save(update_fields = ['position'])
            publish.publish()
            if creating:
                decommission.forget_erase_requests(saved.to_config())
            messages.success(request, 'Saved %s. Buildbot picks the change up within a few seconds.' % saved.name)
            if saved.enabled:
                for level, text in workerctl.create_and_start(saved):
                    messages.add_message(request, getattr(messages, level.upper()), text)
            return redirect('repo_edit', key = saved.key)
    else:
        form    = RepoProfileForm(instance = profile)
        formset = BranchFormSet(instance = profile, prefix = 'branch')

    checks, state, state_text, missing, worker_rows = [], None, '', [], []
    if profile.pk:
        config = profile.to_config()
        checks = health.check(config, settings.WORKERS_DIR)
        worker_rows = health.worker_rows(config, settings.WORKERS_DIR, worker_states())
        state, state_text = publish.sync_state()
        missing = [w for w in config.workers if not os.path.exists(os.path.join(settings.WORKERS_DIR, w, 'buildbot.tac'))] if settings.WORKERS_DIR else []
    return render(request, 'repos/edit.html', {
        'form': form, 'formset': formset, 'profile': profile, 'creating': not profile.pk,
        'sections': form.sections(), 'checks': checks, 'sync_state': state, 'sync_text': state_text,
        'missing_workers': missing, 'worker_rows': worker_rows,
        'workers_to_fix': any(r['state'] in ('stopped', 'not_created') for r in worker_rows),
        'worker_summary': health.worker_summary(worker_rows) if worker_rows else '',
    })


@staff_required
@require_POST
def repo_probe(request):
    """The add and edit forms ask this while the administrator types: what is in the clone folder, and what to suggest."""
    return JsonResponse(probe.probe(request.POST.get('repo_dir', ''), request.POST.get('python', '')))


@staff_required
@require_POST
def repo_workers(request, key):
    """The button on the Workers card: make the missing worker folders and start the workers that are not running."""
    profile = find(key)
    if not profile.enabled:
        messages.warning(request, '%s is disabled. Enable it first.' % profile.name)
        return redirect('repo_edit', key = key)
    notes = workerctl.create_and_start(profile, explicit = True)
    for level, text in notes:
        messages.add_message(request, getattr(messages, level.upper()), text)
    if not notes:
        messages.info(request, 'Nothing to do: every worker has its folder here and is running.')
    return redirect('repo_edit', key = key)


@staff_required
@require_POST
def repo_toggle(request, key):
    profile = find(key)
    profile.enabled = not profile.enabled
    profile.save(update_fields = ['enabled'])
    publish.publish()
    if profile.enabled:
        decommission.forget_erase_requests(profile.to_config())
        messages.success(request, '%s is now enabled.' % profile.name)
    else:
        for line in decommission.stop_work(profile.to_config(), 'The repository %s was disabled by %s' % (profile.key, request.user.username)):
            messages.info(request, line)
        messages.success(request, '%s is now disabled. Its runs were stopped; its results are kept.' % profile.name)
    return redirect('repo_list')


@staff_required
@require_http_methods(['GET', 'POST'])
def repo_delete(request, key):
    profile = find(key)
    config  = profile.to_config()
    if request.method == 'POST':
        if request.POST.get('confirm') != profile.key:
            messages.error(request, 'To delete %s, type its key (%s) in the box.' % (profile.name, profile.key))
        else:
            report = decommission.retire(profile, request.user.username, stop = True,
                                         workers = bool(request.POST.get('workers')),
                                         results = bool(request.POST.get('results')),
                                         history = bool(request.POST.get('history')))
            messages.success(request, report[0] if len(report) == 1 else 'Deleted %s.' % config.name)
            for line in report:
                messages.info(request, line)
            return redirect('repo_list')
    return render(request, 'repos/delete.html', {
        'profile': profile, 'config': config, 'facts': decommission.describe(config),
        'workers_default': request.method != 'POST' or bool(request.POST.get('workers')),
        'builders': decommission.builder_names(config)})


@staff_required
@require_POST
def branch_add(request, key):
    """Add one branch to a repo (from the repo's own page). Its nightly and on-request schedulers appear in Buildbot."""
    profile = find(key)
    back    = redirect('repo_runs', key = key)
    form    = BranchForm(request.POST)
    if not form.is_valid():
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, 'Branch %s: %s' % (form.fields[field].label.lower(), error))
        return back
    data     = form.cleaned_data
    existing = profile.branch_list()
    new      = Branch(repo = profile, name = data['name'], scheduled = data['scheduled'],
                      cron_hour = data['cron_hour'], cron_minute = data['cron_minute'], position = len(existing))
    problems = [message for field, message in hub_repos.validate(profile.to_config(branches = existing + [new]), taken_workers = ())
                if field == 'branches']
    if problems:
        for message in problems:
            messages.error(request, message)
        return back
    new.save()
    publish.publish()
    when = ''
    if new.scheduled and profile.schedule:
        hour, minute = profile.to_config().branch_cron(new.name)
        when = ' It is tested every night (hour %s, minute %s).' % (hour, minute)
    messages.success(request, 'Added the branch %s.%s Buildbot has its schedulers within seconds. Press Run tests to test it now.' % (new.name, when))
    return redirect('%s?branch=%s' % (reverse('repo_runs', args = [key]), new.name))
