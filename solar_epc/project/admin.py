from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    Battery,
    Customer,
    Inverter,
    Project,
    SiteAssessment,
    SolarPanel,
    SystemRecommendation,
    User,
)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "get_full_name", "role", "email", "is_active", "is_staff")
    list_filter = ("role", "is_active", "is_staff")
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Role", {"fields": ("role", "phone_number")}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Role", {"fields": ("role", "phone_number")}),
    )


class ProjectInline(admin.TabularInline):
    model = Project
    extra = 0
    fields = ("project_id", "status", "assigned_rep", "created_at")
    readonly_fields = ("project_id", "created_at")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        "customer_id",
        "full_name",
        "customer_type",
        "county",
        "phone_number",
        "assigned_rep",
        "created_at",
    )
    list_filter = ("customer_type", "county")
    search_fields = ("customer_id", "full_name", "phone_number", "email")
    readonly_fields = ("customer_id", "created_at", "registered_by")
    inlines = [ProjectInline]


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("project_id", "customer", "status", "assigned_rep", "created_at")
    list_filter = ("status",)
    search_fields = ("project_id", "customer__full_name", "customer__customer_id")
    readonly_fields = ("project_id", "created_at")


@admin.register(SiteAssessment)
class SiteAssessmentAdmin(admin.ModelAdmin):
    list_display = (
        "project",
        "status",
        "scheduled_date",
        "assigned_engineer",
        "assessed_by",
        "completed_at",
    )
    list_filter = ("status", "roof_type", "shading_level")
    search_fields = ("project__project_id", "project__customer__full_name")
    readonly_fields = ("scheduled_at", "scheduled_by", "assessed_by", "completed_at")
    fieldsets = (
        ("Scheduling", {
            "fields": ("project", "status", "scheduled_date", "assigned_engineer", "scheduling_notes", "scheduled_by", "scheduled_at"),
        }),
        ("Roof", {
            "fields": ("roof_type", "roof_material", "roof_area_sqm", "roof_orientation", "roof_tilt_degrees", "roof_condition", "structural_integrity_ok", "structural_notes"),
        }),
        ("Shading", {"fields": ("shading_level", "shading_notes")}),
        ("Electrical", {
            "fields": ("current_supply_type", "electrical_panel_capacity_amps", "average_monthly_consumption_kwh", "has_existing_backup", "backup_notes"),
        }),
        ("Access & space", {"fields": ("available_installation_area_sqm", "roof_access_notes")}),
        ("Outcome", {"fields": ("recommended_system_size_kw", "assessor_notes", "assessed_by", "completed_at")}),
    )


class _EquipmentAdmin(admin.ModelAdmin):
    """Shared admin config for the three catalog models — kept here mainly
    as a superuser-level fallback; day-to-day catalog editing happens
    through the in-app /catalog/ pages in views.py."""
    list_filter = ("is_active",)
    search_fields = ("name", "brand", "model_number")


@admin.register(SolarPanel)
class SolarPanelAdmin(_EquipmentAdmin):
    list_display = ("__str__", "wattage_w", "unit_price_kes", "stock_quantity", "is_active")


@admin.register(Inverter)
class InverterAdmin(_EquipmentAdmin):
    list_display = ("__str__", "capacity_kw", "phase", "is_hybrid", "unit_price_kes", "stock_quantity", "is_active")


@admin.register(Battery)
class BatteryAdmin(_EquipmentAdmin):
    list_display = ("__str__", "capacity_kwh", "chemistry", "unit_price_kes", "stock_quantity", "is_active")


@admin.register(SystemRecommendation)
class SystemRecommendationAdmin(admin.ModelAdmin):
    list_display = (
        "site_assessment",
        "target_system_size_kw",
        "panel",
        "panel_quantity",
        "inverter",
        "battery",
        "battery_quantity",
        "calculated_at",
    )
    readonly_fields = ("calculated_at",)
    search_fields = ("site_assessment__project__project_id",)