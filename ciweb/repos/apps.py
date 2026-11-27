from django.apps import AppConfig


class ReposConfig(AppConfig):
    name  = 'ciweb.repos'
    label = 'repos'
    verbose_name = 'Repo profiles'

    def ready(self):
        from ciweb.repos import signals  # noqa: F401
