from django.db import transaction
from django.db.models.signals import post_delete, post_save

from ciweb.repos.models import Branch, RepoProfile


def schedule_publish(**kwargs):
    from ciweb.repos import publish
    transaction.on_commit(publish.publish)


for _model in (RepoProfile, Branch):
    post_save.connect(schedule_publish, sender = _model, dispatch_uid = 'publish-save-%s' % _model.__name__)
    post_delete.connect(schedule_publish, sender = _model, dispatch_uid = 'publish-delete-%s' % _model.__name__)
