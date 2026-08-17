from django.urls import path

from accounts.views import Login, Me, Refresh, Register, Settings

urlpatterns = [
    path("register/", Register.as_view(), name="auth-register"),
    path("login/", Login.as_view(), name="auth-login"),
    path("refresh/", Refresh.as_view(), name="auth-refresh"),
    path("me/", Me.as_view(), name="auth-me"),
    path("settings/", Settings.as_view(), name="auth-settings"),
]
