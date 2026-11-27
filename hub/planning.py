"""Which tests a run covers."""
from hub import selection
from hub.store import count_statuses  # noqa: F401  (the runner commands import it from here)

PLAN_KEYS = ('mode', 'specs', 'base', 'changed_files', 'unmapped', 'ignored', 'full', 'unmatched_specs')


def make_plan(tests_spec, everything, catalog, store, git, adapter, branch = None, base = None):
    """Decide the tests of a run. `tests_spec` is what the person asked for (modules, test ids, globs, `all`);
    without it, `everything` selects all of them, and otherwise the tests are chosen from the files that changed
    since the last scheduled run (only for suites that have a map from files to tests; the others run in full)."""
    specs = selection.split_specs(tests_spec)
    plan  = {'mode': None, 'specs': specs, 'changed_files': [], 'base': None,
             'reasons': {}, 'unmapped': [], 'ignored': [], 'full': False,
             'unmatched_specs': [], 'tests': []}

    if specs:
        plan['mode'] = 'all' if specs == ['all'] else 'explicit'
        plan['tests'], plan['unmatched_specs'] = selection.expand_specs(specs, catalog)
        return plan

    plan['tests'] = [t['id'] for t in catalog['tests']]
    if everything:
        plan['mode'] = 'all'
        return plan

    plan['mode'] = 'impact'
    map_path = adapter.impact_map_path()
    branch   = branch or git.current_branch()
    last     = store.last_tested(branch) if store and branch else None
    base     = base or selection.resolve_base(git.path, (last or {}).get('revision'))
    plan['base'] = base
    if not map_path or not base:
        plan['full'] = True
        why = 'no base revision to compare against' if map_path else 'this suite has no map from files to tests'
        plan['reasons'] = {i: [why] for i in plan['tests']}
        return plan

    files  = selection.changed_files(git.path, base)
    impact = selection.load_impact_map(map_path)
    result = selection.impacted(files, impact, catalog)
    plan.update({'changed_files': files, 'reasons': result['reasons'], 'unmapped': result['unmapped'],
                 'ignored': result['ignored'], 'full': result['full'], 'tests': result['tests']})
    return plan


def selection_record(plan):
    record = {k: plan[k] for k in PLAN_KEYS}
    record['reasons'] = {i: plan['reasons'].get(i, []) for i in plan['tests']}
    return record


def assign_shards(modules, limit):
    count  = max(1, min(len(modules), limit))
    shards = [{'index': i + 1, 'modules': modules[i::count]} for i in range(count)]
    return [dict(s, state = 'pending', worker = None, build = None) for s in shards if s['modules']]
