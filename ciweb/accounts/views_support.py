from django.conf import settings


def public_link(path):
    return settings.PUBLIC_URL.rstrip('/') + path
