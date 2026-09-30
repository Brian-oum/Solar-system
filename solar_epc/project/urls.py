from django.urls import path

from . import views

urlpatterns = [
    path("", views.login_view, name="login"),
    path("register/", views.register_view, name="register"),
    path("logout/", views.logout_view, name="logout"),
    path("dashboard", views.dashboard, name="dashboard"),
    path("dashboard/data/", views.dashboard_data, name="dashboard_data"),
    path("clients/", views.client_list, name="client_list"),
    path("clients/new/", views.client_register, name="client_register"),
    path("clients/<str:customer_id>/", views.client_detail, name="client_detail"),
    path("clients/<str:customer_id>/site-assessment/schedule/", views.site_assessment_schedule, name="site_assessment_schedule"),
    path("clients/<str:customer_id>/site-assessment/survey/", views.site_assessment_survey, name="site_assessment_survey"),
    path("clients/<str:customer_id>/site-assessment/recommendation/", views.site_assessment_recommendation, name="site_assessment_recommendation"),

    # Phase 7: Quotation
    path("quotations/", views.quotation_list, name="quotation_list"),
    path("clients/<str:customer_id>/quotation/edit/", views.quotation_edit, name="quotation_edit"),
    path("clients/<str:customer_id>/quotation/", views.quotation_detail, name="quotation_detail"),
    path("clients/<str:customer_id>/quotation/print/", views.quotation_print, name="quotation_print"),
    path("clients/<str:customer_id>/quotation/download/", views.quotation_download, name="quotation_download"),
    path("clients/<str:customer_id>/quotation/send/", views.quotation_send, name="quotation_send"),
    path("clients/<str:customer_id>/quotation/approve/", views.quotation_approve, name="quotation_approve"),
    path("clients/<str:customer_id>/quotation/reject/", views.quotation_reject, name="quotation_reject"),

    # Equipment catalog
    path("catalog/", views.catalog_home, name="catalog_home"),
    path("catalog/categories/new/", views.category_create, name="category_create"),
    path("catalog/<str:kind>/", views.equipment_list, name="equipment_list"),
    path("catalog/<str:kind>/new/", views.equipment_form, name="equipment_create"),
    path("catalog/<str:kind>/<int:pk>/edit/", views.equipment_form, name="equipment_edit"),
    path("catalog/<str:kind>/<int:pk>/delete/", views.equipment_delete, name="equipment_delete"),
]