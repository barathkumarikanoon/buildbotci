from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class CIBackend(ModelBackend):
    def authenticate(self, request, username = None, password = None, **kwargs):
        if username is None or password is None:
            return None
        user = get_user_model().objects.find(username)
        if user is None:
            get_user_model()().set_password(password)
            return None
        if user.check_password(password):
            return user
        return None

    def user_can_authenticate(self, user):
        return True

    def get_user(self, user_id):
        user = super().get_user(user_id)
        return user if user is not None and user.is_active else None
