"""Running one test and keeping everything about it in the run directory, whatever kind of suite it belongs to."""
import contextlib
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
import traceback

from hub import diffing
from hub.store import ERROR, PASSED, now, write_json


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()

    def isatty(self):
        return False


class Prefixed:
    def __init__(self, stream, prefix):
        self.stream  = stream
        self.prefix  = prefix
        self.pending = ''

    def write(self, text):
        self.pending += text
        lines = self.pending.split('\n')
        self.pending = lines.pop()
        if lines:
            self.stream.write(''.join('%s%s\n' % (self.prefix, line) for line in lines))
            self.stream.flush()
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


class Terminated(BaseException):
    """Buildbot stopped or timed out the build: the process got SIGTERM."""


def on_sigterm(signum, frame):
    raise Terminated()


def install_sigterm_handler():
    signal.signal(signal.SIGTERM, on_sigterm)


def exception_info(exc, tree = None):
    """The exception of a failed test as data for the result page. Frames of this program itself are left out."""
    frames = []
    own = os.path.dirname(os.path.abspath(__file__))
    for frame in traceback.extract_tb(exc.__traceback__):
        if not frames and os.path.abspath(frame.filename).startswith(own):
            continue
        path = frame.filename
        if tree and path.startswith(tree + os.sep):
            path = os.path.relpath(path, tree)
        frames.append({'file': path, 'line': frame.lineno, 'func': frame.name, 'code': frame.line})
    return {'type': type(exc).__name__, 'message': str(exc),
            'traceback': ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            'frames': frames}


def execute_test(adapter, session, entry, tdir):
    """Run one test of the catalog. Its stdout, stderr and logging go to `log.txt`; the result is written to
    `result.json`; when the test did not pass, its actual and expected output and its diff are kept next to them."""
    os.makedirs(tdir, exist_ok = True)
    result = {'id': entry['id'], 'module': entry['module'], 'name': entry['name'],
              'output': entry.get('output'), 'status': ERROR, 'reason': None,
              'started': now(), 'finished': None, 'duration': None,
              'error': None, 'diff': None, 'hunks': [], 'actual_bytes': None,
              'expected_bytes': None, 'kind': adapter.result_kind, 'reproduce': None}
    started = time.time()
    scratch = tempfile.TemporaryDirectory(dir = tdir, prefix = '.scratch-')
    outcome = None
    handler = None
    try:
        with open(os.path.join(tdir, 'log.txt'), 'w', encoding = 'utf8', buffering = 1) as log:
            handler = logging.StreamHandler(log)
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
            logging.getLogger().addHandler(handler)
            out, err = Tee(sys.stdout, log), Tee(sys.stderr, log)
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    outcome = adapter.run_test(session, entry, scratch.name)
                result['status'] = outcome.status
                result['reason'] = outcome.reason
                result.update(outcome.extra)
                if outcome.diff is not None:
                    result['diff']  = outcome.stats
                    result['hunks'] = outcome.hunks
                    with open(os.path.join(tdir, 'diff.txt'), 'w', encoding = 'utf8') as handle:
                        handle.write(outcome.diff)
            except Exception as exc:
                result['status'] = ERROR
                result['error']  = exception_info(exc, adapter.tree)
                result['reason'] = '%s: %s' % (type(exc).__name__, str(exc)[:300])
                log.write('\n' + result['error']['traceback'])
            finally:
                logging.getLogger().removeHandler(handler)
    finally:
        if result['status'] != PASSED and outcome is not None:
            keep(outcome.actual, os.path.join(tdir, 'actual'), scratch.name)
            keep(outcome.expected, os.path.join(tdir, 'expected'), scratch.name)
        scratch.cleanup()

    try:
        result['reproduce'] = adapter.reproduce(entry)
        if result['status'] == 'no_expected':
            result['baseline'] = adapter.baseline_note(entry)
    except Exception:
        pass
    result['actual_bytes']   = diffing.file_size(os.path.join(tdir, 'actual'))
    result['expected_bytes'] = diffing.file_size(os.path.join(tdir, 'expected'))
    result['finished'] = now()
    result['duration'] = round(time.time() - started, 2)
    write_json(os.path.join(tdir, 'result.json'), result)
    return result


def keep(source, dest, scratch):
    if not source or not os.path.exists(source):
        return
    if os.path.abspath(source).startswith(os.path.abspath(scratch) + os.sep):
        shutil.move(source, dest)
    else:
        shutil.copyfile(source, dest)


def saved_files_lines(directory, repo_key, run_id, test_id, status):
    """The lines printed under a failed test in the build log: where its complete files are, and how to open them."""
    if status == PASSED:
        return []
    lines = []
    for name, label in (('diff.txt', 'full diff'), ('expected', 'expected'), ('actual', 'actual')):
        path = os.path.join(directory, name)
        size = diffing.file_size(path)
        if size is not None:
            lines.append('    %-9s %s  (%s)' % (label + ':', path, diffing.human_size(size)))
    ui = os.environ.get('CI_UI_URL')
    if ui and lines:
        page = '%s/r/%s/run/%s/test/%s' % (ui.rstrip('/'), repo_key, run_id, test_id)
        lines.append('    open it:  %s' % page)
        if os.path.exists(os.path.join(directory, 'diff.txt')):
            lines.append('    download the full diff:  %s/raw/diff?download=1' % page)
    return lines
