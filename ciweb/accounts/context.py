from django.contrib.auth import get_user_model


def pending(request):
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated or not user.is_staff:
        return {}
    return {'pending_count': get_user_model().objects.filter(is_active = False, approved = False).count()}
