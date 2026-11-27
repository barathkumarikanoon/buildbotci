from django.urls import path

from ciweb.repos import views

urlpatterns = [
    path('settings/repos', views.repo_list, name = 'repo_list'),
    path('settings/repos/new', views.repo_edit, name = 'repo_new'),
    path('settings/repos/probe', views.repo_probe, name = 'repo_probe'),
    path('settings/repos/<key>', views.repo_edit, name = 'repo_edit'),
    path('settings/repos/<key>/toggle', views.repo_toggle, name = 'repo_toggle'),
    path('settings/repos/<key>/workers', views.repo_workers, name = 'repo_workers'),
    path('settings/repos/<key>/branches/add', views.branch_add, name = 'branch_add'),
    path('settings/repos/<key>/delete', views.repo_delete, name = 'repo_delete'),
]
