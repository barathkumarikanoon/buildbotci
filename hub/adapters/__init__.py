"""The kinds of test suite the CI understands. Add a class to ADAPTERS to support another."""
from hub.adapters.base import Adapter, Field, Outcome, Session
from hub.adapters.command import CommandAdapter
from hub.adapters.comparison import OutputComparisonAdapter
from hub.adapters.pytest_suite import PytestAdapter

ADAPTERS = {cls.kind: cls for cls in (OutputComparisonAdapter, PytestAdapter, CommandAdapter)}


def adapter_class(kind):
    from hub.repos import ADAPTER_ALIASES
    try:
        return ADAPTERS[ADAPTER_ALIASES.get(kind, kind)]
    except KeyError:
        raise SystemExit('Unknown test suite type %r (known: %s)' % (kind, ', '.join(sorted(ADAPTERS))))


def make_adapter(repo, tree = None):
    """The adapter of a repo profile, working in `tree` (a checkout) or in the configured clone."""
    return adapter_class(repo.adapter)(repo, tree or repo.repo_dir, repo.options)
