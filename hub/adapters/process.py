"""Run an external command for a test: its output goes live to the test's log, and a copy is kept in a file."""
import os
import signal
import subprocess
import threading


def run_streaming(command, cwd, env, output_path, timeout, shell = False):
    """Run `command` and print each line it writes as it comes (the caller's stdout is the test log), also writing
    them to `output_path`. Returns (exit code, timed_out). The whole process group is stopped on a timeout, and
    also when this process is terminated (Buildbot stop), so no child outlives the test."""
    proc = subprocess.Popen(command, cwd = cwd, env = env, shell = shell, stdin = subprocess.DEVNULL,
                            stdout = subprocess.PIPE, stderr = subprocess.STDOUT, start_new_session = True)

    def pump():
        with open(output_path, 'wb') as sink:
            for line in iter(proc.stdout.readline, b''):
                sink.write(line)
                sink.flush()
                print(line.decode('utf8', 'replace'), end = '', flush = True)

    reader = threading.Thread(target = pump, daemon = True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout = timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_group(proc)
    except BaseException:
        stop_group(proc)
        raise
    finally:
        reader.join(10)
        proc.stdout.close()
    return proc.returncode, timed_out


def stop_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout = 10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    except (ProcessLookupError, PermissionError):
        pass
