from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordResetConfirmView, PasswordResetView
from django.core import signing
from django.core.mail import send_mail
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST

from ciweb.accounts import lockout, notify
from ciweb.accounts.forms import ForgotForm, SignupForm
from ciweb.accounts.views_support import public_link

VERIFY_SALT  = 'ci-verify-email'
VERIFY_HOURS = 48
NO_MAIL = ('Password reset by email is not set up here. Ask an administrator for a reset link '
           '(python manage.py ci_users reset YOUR_NAME).')


def email_domain(email):
    return email.rsplit('@', 1)[-1].lower()


def send_verification(user):
    token = signing.dumps({'u': user.pk}, salt = VERIFY_SALT)
    body  = render_to_string('accounts/verify_email.txt', {
        'user': user, 'hours': VERIFY_HOURS, 'link': public_link(reverse('verify', args = [token]))})
    try:
        send_mail('Confirm your email for %s' % settings.SITE_NAME, body, None, [user.email])
    except OSError:
        return False
    return True


def signup(request):
    form = SignupForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        notify.new_signup(user)
        if settings.MAIL_CONFIGURED:
            send_verification(user)
            messages.info(request, 'Account created. Check your email and open the link to confirm your address. '
                                   'An administrator may still need to approve the account.')
        else:
            messages.info(request, 'Account created. An administrator must approve it before you can sign in.')
        return redirect('login')
    return render(request, 'accounts/signup.html', {'form': form}, status = 400 if form.errors else 200)


def verify(request, token):
    try:
        data = signing.loads(token, salt = VERIFY_SALT, max_age = VERIFY_HOURS * 3600)
        user = get_user_model().objects.get(pk = data['u'])
    except (signing.BadSignature, get_user_model().DoesNotExist, KeyError):
        messages.error(request, 'That confirmation link is not valid or has expired.')
        return redirect('login')
    user.email_verified = True
    activate = not user.is_active and not user.approved and email_domain(user.email) in settings.SIGNUP_DOMAINS
    if activate:
        user.is_active = True
        user.approved  = True
    user.save(update_fields = ['email_verified', 'is_active', 'approved'])
    if activate:
        messages.info(request, 'Your email is confirmed and your account is active. You can sign in now.')
    else:
        messages.info(request, 'Your email is confirmed. An administrator must still approve your account.')
    return redirect('login')


@login_required
def account(request):
    return render(request, 'accounts/account.html')


class ForgotView(PasswordResetView):
    form_class = ForgotForm
    template_name = 'registration/password_reset_form.html'
    email_template_name = 'registration/password_reset_email.html'
    subject_template_name = 'registration/password_reset_subject.txt'
    success_url = reverse_lazy('password_reset_done')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['mail_ready'] = settings.MAIL_CONFIGURED
        return context

    def form_valid(self, form):
        if not settings.MAIL_CONFIGURED:
            messages.info(self.request, NO_MAIL)
            return redirect('login')
        self.extra_email_context = {'protocol': settings.PUBLIC_URL.split('://')[0] if '://' in settings.PUBLIC_URL else 'https',
                                    'domain': settings.PUBLIC_URL.split('://', 1)[-1].rstrip('/')}
        return super().form_valid(form)


class ResetView(PasswordResetConfirmView):
    success_url = reverse_lazy('password_reset_complete')

    def form_valid(self, form):
        response = super().form_valid(form)
        get_user_model().objects.filter(pk = form.user.pk).update(email_verified = True)
        return response


def staff_required(view):
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            return HttpResponseForbidden(render_to_string('accounts/not_allowed.html', request = request))
        return view(request, *args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def person_row(user):
    return {'id': user.pk, 'username': user.username, 'email': user.email, 'state': user.state,
            'email_verified': user.email_verified, 'staff': user.is_staff, 'locked': lockout.is_locked(user),
            'joined': user.date_joined, 'last_login': user.last_login}


@staff_required
def people(request):
    users   = get_user_model().objects.order_by('username')
    waiting = [person_row(u) for u in users.filter(is_active = False, approved = False).order_by('date_joined')]
    others  = [person_row(u) for u in users.exclude(is_active = False, approved = False)]
    return render(request, 'accounts/people.html', {'waiting': waiting, 'others': others, 'me': request.user.pk})


@staff_required
@require_POST
def person_action(request, pk, action):
    target = get_user_model().objects.filter(pk = pk).first()
    if target is None:
        messages.error(request, 'That account no longer exists.')
        return redirect('people')
    name = target.username
    if action == 'approve' and not target.is_active:
        target.approve()
        notify.approved(target)
        messages.success(request, '%s is approved and can sign in now.' % name)
    elif action == 'reject' and target.state == 'pending':
        notify.rejected(target)
        target.delete()
        messages.info(request, 'The sign-up of %s was rejected and removed.' % name)
    elif action == 'disable' and target.pk != request.user.pk and target.is_active:
        target.disable()
        messages.info(request, '%s is disabled and was signed out.' % name)
    elif action == 'unlock':
        lockout.clear(target)
        messages.success(request, '%s is unlocked.' % name)
    else:
        messages.error(request, 'That action is not possible for %s.' % name)
    return redirect('people')
