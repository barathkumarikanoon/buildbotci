from django.contrib.auth import views as auth_views
from django.urls import path

from ciweb.accounts import views
from ciweb.accounts.forms import LoginForm

urlpatterns = [
    path('login', auth_views.LoginView.as_view(authentication_form = LoginForm, redirect_authenticated_user = True), name = 'login'),
    path('logout', auth_views.LogoutView.as_view(), name = 'logout'),
    path('signup', views.signup, name = 'signup'),
    path('verify/<token>', views.verify, name = 'verify'),
    path('account', views.account, name = 'account'),
    path('people', views.people, name = 'people'),
    path('people/<int:pk>/<action>', views.person_action, name = 'person_action'),
    path('account/password', auth_views.PasswordChangeView.as_view(), name = 'password_change'),
    path('account/password/done', auth_views.PasswordChangeDoneView.as_view(), name = 'password_change_done'),
    path('forgot', views.ForgotView.as_view(), name = 'password_reset'),
    path('forgot/sent', auth_views.PasswordResetDoneView.as_view(), name = 'password_reset_done'),
    path('reset/<uidb64>/<token>', views.ResetView.as_view(), name = 'password_reset_confirm'),
    path('reset/done', auth_views.PasswordResetCompleteView.as_view(), name = 'password_reset_complete'),
]
