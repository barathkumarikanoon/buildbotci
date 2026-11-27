"""One login for many scripts: after a website superuser has logged in once, the scripts in bin/ do not ask again in the
same terminal until the login runs out.

The login is a small signed file, one for each terminal (the session of the terminal, `os.getsid(0)`, which every script
started from that terminal shares), kept where only the account can read it: $XDG_RUNTIME_DIR (cleared at logout and
reboot) or ~/.cache/ci. It stops counting when any of these happens:
  - it has not been used for CI_LOGIN_MINUTES (default 30; 0 turns sessions off, so every script asks);
  - 8 hours have passed since the login;
  - the terminal is closed;
  - the account is disabled, is no longer a superuser, or has its password changed (checked on every use);
  - someone changes the file (it is signed with the website's secret).
Anyone who can run commands as the account and read the settings file can make such a file, as they can run the underlying
programs: this is for knowing who did what, not a wall against the operating system account (see CLAUDE.md).
"""
import hashlib
import json
import os
import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing

MAX_HOURS = 8
SALT = 'ci-script-login'


def folder():
    base = os.environ.get('XDG_RUNTIME_DIR')
    return base if base and os.path.isdir(base) else os.path.join(os.path.expanduser('~'), '.cache', 'ci')


def path():
    return os.path.join(folder(), 'ci-login-%d-%d' % (os.getuid(), os.getsid(0)))


def fingerprint(user):
    """Changes when the password does, so a new password ends every login of that account."""
    return hashlib.sha256(user.password.encode()).hexdigest()[:16]


def minutes():
    return max(0, int(getattr(settings, 'SCRIPT_LOGIN_MINUTES', 30)))


def signer():
    return signing.TimestampSigner(key = settings.SECRET_KEY, salt = SALT)


def write(data):
    os.makedirs(folder(), mode = 0o700, exist_ok = True)
    descriptor = os.open(path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(signer().sign(json.dumps(data)))


def end():
    """Forget the login of this terminal. True when there was one."""
    try:
        os.remove(path())
        return True
    except OSError:
        return False


def start(user):
    if not minutes():
        return
    now = time.time()
    write({'user': user.pk, 'uid': os.getuid(), 'sid': os.getsid(0), 'began': now, 'last': now, 'fp': fingerprint(user)})
    sweep()


def sweep():
    """Remove the files of logins that ended long ago (terminals that were closed)."""
    try:
        for name in os.listdir(folder()):
            full = os.path.join(folder(), name)
            if name.startswith('ci-login-%d-' % os.getuid()) and time.time() - os.path.getmtime(full) > MAX_HOURS * 3600:
                os.remove(full)
    except OSError:
        pass


def current(touch = True):
    """The superuser who is logged in in this terminal, or None. Each use renews the idle time (`touch`)."""
    if not minutes():
        return None
    try:
        with open(path(), encoding = 'utf8') as handle:
            data = json.loads(signer().unsign(handle.read(), max_age = MAX_HOURS * 3600))
    except OSError:
        return None
    except (signing.BadSignature, ValueError):
        end()
        return None
    now = time.time()
    if (data.get('uid') != os.getuid() or data.get('sid') != os.getsid(0) or now - data.get('last', 0) > minutes() * 60
            or now - data.get('began', 0) > MAX_HOURS * 3600):
        end()
        return None
    user = get_user_model().objects.filter(pk = data.get('user')).first()
    if not (user and user.is_active and user.is_superuser and fingerprint(user) == data.get('fp')):
        end()
        return None
    if touch:
        data['last'] = now
        write(data)
    return user


def minutes_left():
    """For a status line: minutes until it runs out, or 0."""
    try:
        with open(path(), encoding = 'utf8') as handle:
            data = json.loads(signer().unsign(handle.read(), max_age = MAX_HOURS * 3600))
    except (OSError, signing.BadSignature, ValueError):
        return 0
    return max(0, int(min(data['last'] + minutes() * 60, data['began'] + MAX_HOURS * 3600) - time.time()) // 60 + 1)
