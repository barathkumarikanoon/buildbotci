from urllib.parse import parse_qsl, unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured


# Settings of the connection that may be given in the URL, for example ?sslmode=require
CONNECTION_OPTIONS = ('sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'connect_timeout', 'application_name')


def database_from_url(url, schema = ''):
    if not url:
        return None
    scheme = url.split(':', 1)[0].split('+')[0].lower()
    if scheme == 'sqlite':
        rest = url.split('://', 1)[1] if '://' in url else ''
        name = rest[1:] if rest.startswith('/') else rest
        return {'ENGINE': 'django.db.backends.sqlite3', 'NAME': name or ':memory:'}
    if scheme in ('postgresql', 'postgres'):
        parts  = urlsplit(url)
        config = {'ENGINE': 'django.db.backends.postgresql', 'NAME': unquote(parts.path.lstrip('/')),
                  'USER': unquote(parts.username or ''), 'PASSWORD': unquote(parts.password or ''),
                  'HOST': parts.hostname or '', 'PORT': str(parts.port or '')}
        options = {key: value for key, value in parse_qsl(parts.query) if key in CONNECTION_OPTIONS}
        if schema:
            options['options'] = '-c search_path=%s' % schema
        if options:
            config['OPTIONS'] = options
        return config
    raise ImproperlyConfigured('Only sqlite and postgresql database URLs are supported, got: %s' % scheme)
