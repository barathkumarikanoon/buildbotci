"""Who may run the scripts in bin/ that change something: a website account that is a superuser.

The same rules as signing in to the website (ciweb/accounts/forms.py: LoginForm): the account is found by user name or
email in any letter case, five wrong passwords lock it for 15 minutes, and the words "waiting for approval", "disabled"
and "not a superuser" only appear after the right password.
"""
from django.contrib.auth import authenticate, get_user_model
from django.db.utils import OperationalError, ProgrammingError

from ciweb.accounts import lockout
from ciweb.accounts.forms import DISABLED, LOCKED, PENDING, WRONG

NOT_SUPERUSER = 'This account is not a superuser. Only superusers may run this.'

READY, NO_SUPERUSER, NOT_SET_UP, UNREACHABLE = 'ready', 'no_superuser', 'not_set_up', 'unreachable'


def state():
    """READY when at least one active superuser exists. NO_SUPERUSER and NOT_SET_UP (no tables yet) are a fresh install,
    where nobody can sign in yet. UNREACHABLE is any other database problem: then nobody is let in."""
    try:
        return READY if get_user_model().objects.filter(is_superuser = True, is_active = True).exists() else NO_SUPERUSER
    except ProgrammingError:
        return NOT_SET_UP
    except OperationalError as exc:
        return NOT_SET_UP if 'no such table' in str(exc) else UNREACHABLE


def check(login, password):
    """(user, problem): the superuser, or None and the text to show."""
    users = get_user_model()
    user  = users.objects.find(login)
    if lockout.is_locked(user):
        users().set_password(password)
        return None, LOCKED
    found = authenticate(None, username = login, password = password)
    if found is None:
        if user is not None:
            lockout.register_failure(user)
        return None, WRONG
    lockout.clear(found)
    if not found.is_active:
        return None, DISABLED if found.approved else PENDING
    if not found.is_superuser:
        return None, NOT_SUPERUSER
    return found, ''
