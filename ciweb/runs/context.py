from django.conf import settings


def links(request):
    return {'buildbot_link': settings.BUILDBOT_PUBLIC_URL}
