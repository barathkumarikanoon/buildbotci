from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('ciweb.accounts.urls')),
    path('', include('ciweb.repos.urls')),
    path('', include('ciweb.runs.urls')),
]
