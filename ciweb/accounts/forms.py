from django import forms
from django.contrib.auth import authenticate, get_user_model, password_validation
import smtplib

from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm
from django.core.exceptions import ValidationError

from ciweb.accounts import lockout

LOCKED   = 'Too many wrong attempts. Try again in %d minutes.' % lockout.LOCK_MINUTES
PENDING  = 'Your account is waiting for approval by an administrator.'
DISABLED = 'This account is disabled. Ask an administrator.'
WRONG    = 'Wrong user name or password.'


class LoginForm(AuthenticationForm):
    username = forms.CharField(label = 'User name or email', widget = forms.TextInput(attrs = {'autofocus': True, 'autocomplete': 'username'}))

    error_messages = {'invalid_login': WRONG, 'inactive': DISABLED}

    def clean(self):
        login    = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')
        if login and password:
            user = get_user_model().objects.find(login)
            if lockout.is_locked(user):
                get_user_model()().set_password(password)
                raise ValidationError(LOCKED, code = 'locked')
            self.user_cache = authenticate(self.request, username = login, password = password)
            if self.user_cache is None:
                if user is not None:
                    lockout.register_failure(user)
                raise self.get_invalid_login_error()
            lockout.clear(self.user_cache)
            self.confirm_login_allowed(self.user_cache)
        return self.cleaned_data

    def confirm_login_allowed(self, user):
        if not user.is_active:
            raise ValidationError(DISABLED if user.approved else PENDING, code = 'inactive')


class SignupForm(forms.ModelForm):
    password1 = forms.CharField(label = 'Password', strip = False, widget = forms.PasswordInput(attrs = {'autocomplete': 'new-password'}),
                                help_text = 'At least 10 characters.')
    password2 = forms.CharField(label = 'Password again', strip = False, widget = forms.PasswordInput(attrs = {'autocomplete': 'new-password'}))

    class Meta:
        model  = get_user_model()
        fields = ('username', 'email')
        help_texts = {'username': '3 to 32 characters. This is the name shown on the runs you start.'}

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if get_user_model().objects.filter(email__iexact = email).exists():
            raise ValidationError('That user name or email is already registered.')
        return email

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if get_user_model().objects.filter(username__iexact = username).exists():
            raise ValidationError('That user name or email is already registered.')
        return username

    def clean(self):
        data = super().clean()
        one, two = data.get('password1'), data.get('password2')
        if one and two and one != two:
            self.add_error('password2', 'The two passwords are not the same.')
        elif one:
            candidate = get_user_model()(username = data.get('username') or '', email = data.get('email') or '')
            try:
                password_validation.validate_password(one, candidate)
            except ValidationError as exc:
                self.add_error('password1', exc)
        return data

    def save(self, commit = True):
        user = super().save(commit = False)
        user.set_password(self.cleaned_data['password1'])
        user.is_active = False
        user.approved  = False
        if commit:
            user.save()
        return user


class ForgotForm(PasswordResetForm):
    def send_mail(self, *args, **kwargs):
        try:
            super().send_mail(*args, **kwargs)
        except (OSError, smtplib.SMTPException):
            pass
