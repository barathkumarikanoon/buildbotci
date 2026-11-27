"""Lets the master follow the repo profiles by itself.

The web UI writes the snapshot of the profiles whenever someone saves one. This service looks at that file every few
seconds; when its content differs from the one the master loaded, it reconfigures the master (the same as
`buildbot reconfig`, running builds are not lost) and reports what happened in `profiles.applied.json`, which the web
UI shows next to the profiles."""
import json
import os
import tempfile
import time

from twisted.internet import defer, task
from twisted.python import log

from buildbot.util import service

from hub import repos
from hub.buildbot import cleanup


def file_version(path):
    return repos.text_version(repos.read_snapshot_text(path))


def write_applied(snapshot_path, version, error = None):
    target = repos.applied_path(snapshot_path)
    try:
        os.makedirs(os.path.dirname(target), exist_ok = True)
        fd, tmp = tempfile.mkstemp(dir = os.path.dirname(target), prefix = '.tmp-')
        with os.fdopen(fd, 'w', encoding = 'utf8') as handle:
            json.dump({'version': version, 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'error': error}, handle)
        os.replace(tmp, target)
    except OSError as exc:
        log.msg('profiles watcher: cannot write %s: %s' % (target, exc))


class ProfilesWatcher(service.BuildbotService):
    name = 'profiles-watcher'

    def checkConfig(self, path = '', loaded = '', interval = 10, **kwargs):
        pass

    def reconfigService(self, path = '', loaded = '', interval = 10, **kwargs):
        self.path      = path
        self.loaded    = loaded
        self.interval  = interval
        self.attempted = loaded
        self.swept     = getattr(self, 'swept', False)
        write_applied(path, loaded)

    def startService(self):
        super().startService()
        self.loop = task.LoopingCall(self.poll)
        self.loop.start(self.interval, now = False)

    def stopService(self):
        if getattr(self, 'loop', None) and self.loop.running:
            self.loop.stop()
        return super().stopService()

    @defer.inlineCallbacks
    def poll(self):
        try:
            if not self.swept:
                # once after the master starts: clear what a repo that was removed while it was stopped left behind
                self.swept = True
                yield cleanup.reconcile(self.master, self.path)
            current = file_version(self.path)
            if current == self.loaded or current == self.attempted:
                return
            self.attempted = current
            log.msg('profiles watcher: the repo profiles changed (%s -> %s); reconfiguring' % (self.loaded, current))
            yield self.master.reconfig()
            if self.loaded == current:
                yield cleanup.reconcile(self.master, self.path)
            if self.loaded != current:
                write_applied(self.path, self.loaded, 'The master could not load the new repo profiles and keeps the old ones. '
                                                      'See twistd.log of the master; correct the profile and save again.')
        except Exception as exc:
            log.err(exc, 'profiles watcher')
