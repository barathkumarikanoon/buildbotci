"""What a command knows when it starts: which repo profile, where its results go, which checkout it works in."""
import os

from hub import repos
from hub.adapters import make_adapter
from hub.gitrepo import GitRepo
from hub.store import Store


class Context:
    def __init__(self, args, tree = None):
        path = getattr(args, 'profiles_file', None) or repos.default_snapshot_path()
        if not path:
            raise SystemExit('Cannot find the repo profiles: set CI_PROFILES_FILE or CI_RESULTS_DIR (or pass --profiles-file)')
        self.args         = args
        self.profiles_file = path
        self.repo         = repos.get_repo(path, args.repo)
        self.tree         = os.path.abspath(tree or getattr(args, 'tree', None) or self.repo.repo_dir)
        self.git          = GitRepo(self.repo, path = self.tree)
        self.results_root = args.results_dir
        self.store        = Store(self.repo.results_dir(args.results_dir))
        self._adapter     = None

    @property
    def adapter(self):
        if self._adapter is None:
            self._adapter = make_adapter(self.repo, self.tree)
        return self._adapter

    def verify(self):
        self.git.verify()

    def activate(self):
        """Prepare this process to import and run the code under test (the environment of the repo profile first)."""
        self.verify()
        os.environ.update(self.repo.env)
        self.adapter.activate()

    def for_tree(self, tree):
        return Context(self.args, tree = tree)
