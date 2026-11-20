from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.urls import reverse

from ciweb.accounts.views_support import public_link


def staff_emails():
    return list(get_user_model().objects.filter(is_staff = True, is_active = True).exclude(email = '').values_list('email', flat = True))


def deliver(subject, body, recipients):
    if not settings.MAIL_CONFIGURED or not recipients:
        return False
    try:
        send_mail(subject, body, None, recipients)
    except OSError:
        return False
    return True


def new_signup(user):
    return deliver('New sign-up waiting for approval: %s' % user.username,
                   'Someone signed up for IK Regressions and is waiting for you to approve the account.\n\n'
                   'User name: %s\nEmail: %s\nEmail confirmed: %s\n\nReview and approve: %s\n'
                   % (user.username, user.email, 'yes' if user.email_verified else 'not yet', public_link(reverse('people'))),
                   staff_emails())


def approved(user):
    return deliver('Your IK Regressions account is approved',
                   'Hello %s,\n\nYour account has been approved. You can sign in here:\n\n%s\n'
                   % (user.username, public_link(reverse('login'))), [user.email])


def rejected(user):
    return deliver('Your IK Regressions sign-up was not approved',
                   'Hello %s,\n\nYour sign-up was not approved. If you think this is a mistake, ask an administrator.\n' % user.username,
                   [user.email])
