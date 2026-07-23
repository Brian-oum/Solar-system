from django.urls import path

from . import views

urlpatterns = [
    path("", views.login_view, name="login"),
    path("register/", views.register_view, name="register"),
    path("logout/", views.logout_view, name="logout"),
    path("dashboard", views.dashboard, name="dashboard"),
    path("clients/", views.client_list, name="client_list"),
    path("clients/new/", views.client_register, name="client_register"),
    path("clients/<str:customer_id>/", views.client_detail, name="client_detail"),
    path("clients/<str:customer_id>/site-assessment/schedule/", views.site_assessment_schedule, name="site_assessment_schedule"),
    path("clients/<str:customer_id>/site-assessment/survey/", views.site_assessment_survey, name="site_assessment_survey"),
    path("clients/<str:customer_id>/site-assessment/recommendation/", views.site_assessment_recommendation, name="site_assessment_recommendation"),

    # Equipment catalog (admin only)
    path("catalog/", views.catalog_home, name="catalog_home"),
    path("catalog/<str:kind>/", views.equipment_list, name="equipment_list"),
    path("catalog/<str:kind>/new/", views.equipment_form, name="equipment_create"),
    path("catalog/<str:kind>/<int:pk>/edit/", views.equipment_form, name="equipment_edit"),
    path("catalog/<str:kind>/<int:pk>/delete/", views.equipment_delete, name="equipment_delete"),
]