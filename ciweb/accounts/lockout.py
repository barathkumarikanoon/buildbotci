from datetime import timedelta

from django.utils import timezone

MAX_FAILURES = 5
LOCK_MINUTES = 15


def is_locked(user):
    return bool(user and user.locked_until and user.locked_until > timezone.now())


def register_failure(user):
    user.failed_logins += 1
    if user.failed_logins >= MAX_FAILURES:
        user.failed_logins = 0
        user.locked_until  = timezone.now() + timedelta(minutes = LOCK_MINUTES)
    user.save(update_fields = ['failed_logins', 'locked_until'])


def clear(user):
    if user.failed_logins or user.locked_until:
        user.failed_logins = 0
        user.locked_until  = None
        user.save(update_fields = ['failed_logins', 'locked_until'])
