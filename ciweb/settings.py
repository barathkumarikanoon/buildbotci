import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from django.contrib.messages import constants as message_levels
from django.core.exceptions import ImproperlyConfigured

from ciweb import envfile
from ciweb.db import database_from_url

BASE_DIR  = Path(__file__).resolve().parent.parent
TESTING   = 'test' in sys.argv[1:2] or os.environ.get('CIWEB_TESTING') == '1'
RUNSERVER = 'runserver' in sys.argv[1:2]
if not TESTING:
    envfile.load()


def env(name, default = ''):
    return os.environ.get(name, default)


def required(name, test_value = ''):
    """A setting that has no default: the site refuses to start without it (the unit tests get a fixed value)."""
    value = os.environ.get(name)
    if value:
        return value
    if TESTING:
        return test_value
    raise ImproperlyConfigured('%s is not set. Put it in the settings file (~/bb-master.env).' % name)


SECRET_KEY = env('REG_UI_SECRET')
if not SECRET_KEY:
    if not TESTING:
        raise ImproperlyConfigured('REG_UI_SECRET must be set to a long random value (it signs the login cookies)')
    SECRET_KEY = 'test-only-secret-key-that-is-never-used-in-production'

DEBUG      = env('REG_UI_DEBUG', '1' if RUNSERVER else '0') == '1'
PUBLIC_URL = env('REG_UI_URL')
_public    = urlsplit(PUBLIC_URL)

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]'] + ([_public.hostname] if _public.hostname else []) \
    + [h.strip() for h in env('REG_UI_ALLOWED_HOSTS').split(',') if h.strip()]
CSRF_TRUSTED_ORIGINS = ['%s://%s' % (_public.scheme, _public.netloc)] if _public.scheme and _public.netloc else []

INSTALLED_APPS = [
    'ciweb',
    'ciweb.accounts',
    'ciweb.runs',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF     = 'ciweb.urls'
WSGI_APPLICATION = 'ciweb.wsgi.application'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'ciweb' / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
        'ciweb.accounts.context.pending',
    ]},
}]

DB_URL = env('REG_UI_DB_URL') or env('BUILDBOT_DB_URL') or ('sqlite://' if TESTING else '')
_default_db = database_from_url(DB_URL, env('REG_UI_DB_SCHEMA'))
if _default_db is None:
    raise ImproperlyConfigured('Set REG_UI_DB_URL (or BUILDBOT_DB_URL) to the database that holds the accounts')
if not TESTING:
    _default_db.setdefault('CONN_MAX_AGE', 60)
    _default_db.setdefault('CONN_HEALTH_CHECKS', True)
DATABASES = {'default': _default_db}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'accounts.User'
AUTHENTICATION_BACKENDS = ['ciweb.accounts.backends.CIBackend']
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.Argon2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2PasswordHasher',
]
if TESTING:
    PASSWORD_HASHERS = ['ciweb.accounts.testing.FastArgon2PasswordHasher']
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'ciweb.accounts.validators.ContainsUserDetailsValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 10}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LOGIN_URL           = 'login'
LOGIN_REDIRECT_URL  = '/'
LOGOUT_REDIRECT_URL = '/'
PASSWORD_RESET_TIMEOUT = 3600

_secure_cookies = env('REG_UI_SECURE_COOKIES') == '1'
SESSION_COOKIE_AGE      = 12 * 3600
SESSION_COOKIE_SECURE   = _secure_cookies
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SECURE      = _secure_cookies
CSRF_COOKIE_SAMESITE    = 'Lax'
SECURE_REFERRER_POLICY  = 'same-origin'
SECURE_CONTENT_TYPE_NOSNIFF = True
if env('REG_UI_BEHIND_PROXY') == '1':
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_HSTS_SECONDS = int(env('REG_UI_HSTS_SECONDS', '0'))
SECURE_SSL_REDIRECT = env('REG_UI_SSL_REDIRECT') == '1'
X_FRAME_OPTIONS = 'DENY'

USE_TZ    = True
TIME_ZONE = env('TZ', 'UTC')
USE_I18N  = False

STATIC_URL  = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
# Serve the collected files (python manage.py collectstatic, done by bin/run-ui). While developing, or when that folder
# does not exist, read the files from the apps instead so that the pages never lose their style sheet.
WHITENOISE_USE_FINDERS = DEBUG or not STATIC_ROOT.exists()

LOG_LEVEL = env('REG_UI_LOG_LEVEL', 'INFO').upper()
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'plain': {'format': '%(asctime)s %(levelname)s %(name)s: %(message)s'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'plain'}},
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'django.request': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'django.security': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'ciweb': {'handlers': ['console'], 'level': LOG_LEVEL, 'propagate': False},
    },
}

CSRF_FAILURE_VIEW = 'ciweb.runs.views.csrf_failed'

MESSAGE_TAGS = {message_levels.SUCCESS: 'ok'}

EMAIL_HOST          = env('REG_SMTP_HOST')
EMAIL_PORT          = int(env('REG_SMTP_PORT', '587'))
EMAIL_HOST_USER     = env('REG_SMTP_USER')
EMAIL_HOST_PASSWORD = env('REG_SMTP_PASSWORD')
_security           = env('REG_SMTP_SECURITY', 'starttls').lower()
EMAIL_USE_TLS       = _security == 'starttls'
EMAIL_USE_SSL       = _security == 'ssl'
EMAIL_TIMEOUT       = 20
DEFAULT_FROM_EMAIL  = env('REG_SMTP_FROM')
MAIL_CONFIGURED     = bool(EMAIL_HOST and DEFAULT_FROM_EMAIL and PUBLIC_URL)
EMAIL_BACKEND       = 'django.core.mail.backends.smtp.EmailBackend' if MAIL_CONFIGURED else 'django.core.mail.backends.dummy.EmailBackend'

RESULTS_DIR       = os.path.expanduser(os.path.expandvars(required('REG_RESULTS_DIR', '~/regression-results')))
BUILDBOT_URL      = env('REG_BUILDBOT_URL')
BUILDBOT_USER     = env('REG_BUILDBOT_USER')
BUILDBOT_PASS     = env('REG_BUILDBOT_PASSWORD') or env('BUILDBOT_UI_PASSWORD')   # the same account as the master's regression-ui login
FORCE_SCHEDULER   = env('REG_BUILDBOT_FORCE_SCHEDULER', 'force-regression')
CATALOG_SCHEDULER = env('REG_BUILDBOT_CATALOG_SCHEDULER', 'force-catalog')
BRANCHES          = tuple(b.strip() for b in required('REG_BRANCHES', 'main,2026').split(',') if b.strip())
SIGNUP_DOMAINS    = tuple(d.strip().lower() for d in env('REG_SIGNUP_DOMAINS').split(',') if d.strip())
AUTO_NAME         = 'Auto'
