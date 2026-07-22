from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views


urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("register/", views.SignUpView.as_view(), name="register"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/new/", views.EngineerCreateView.as_view(), name="engineer_create"),
    path("", views.dashboard, name="dashboard"),
]