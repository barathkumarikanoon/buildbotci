from django.urls import path

from ciweb.runs import views

urlpatterns = [
    path('', views.index, name = 'index'),
    path('r/<key>/', views.repo_runs, name = 'repo_runs'),
    path('r/<key>/run/<run_id>', views.run_view, name = 'run_view'),
    path('r/<key>/run/<run_id>/test/<test_id>', views.test_view, name = 'test_view'),
    path('r/<key>/run/<run_id>/test/<test_id>/raw/<kind>', views.raw_view, name = 'raw_view'),
    path('r/<key>/api/run/<run_id>', views.api_run, name = 'api_run'),
    path('r/<key>/api/run/<run_id>/tail', views.api_tail, name = 'api_tail'),
    path('r/<key>/run/<run_id>/stop', views.stop_run, name = 'stop_run'),
    path('r/<key>/new', views.new_run, name = 'new_run'),
    path('r/<key>/catalog/refresh', views.refresh_catalog, name = 'refresh_catalog'),
    path('run/<run_id>', views.legacy_run, name = 'legacy_run'),
    path('healthz', views.healthz, name = 'healthz'),
]
