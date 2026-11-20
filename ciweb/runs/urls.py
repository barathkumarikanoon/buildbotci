from django.urls import path

from ciweb.runs import views

urlpatterns = [
    path('', views.index, name = 'index'),
    path('run/<run_id>', views.run_view, name = 'run_view'),
    path('run/<run_id>/test/<test_id>', views.test_view, name = 'test_view'),
    path('run/<run_id>/test/<test_id>/raw/<kind>', views.raw_view, name = 'raw_view'),
    path('api/run/<run_id>', views.api_run, name = 'api_run'),
    path('api/run/<run_id>/tail', views.api_tail, name = 'api_tail'),
    path('new', views.new_run, name = 'new_run'),
    path('catalog/refresh', views.refresh_catalog, name = 'refresh_catalog'),
    path('healthz', views.healthz, name = 'healthz'),
]
