from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """
    Two roles for now, matching the access model we designed:
      - SITE_ENGINEER: full lifecycle ownership of their own assigned projects
      - ADMIN: system-wide visibility, user management, oversight
    """

    class Role(models.TextChoices):
        SITE_ENGINEER = "site_engineer", "Site engineer"
        ADMIN = "admin", "Admin"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.SITE_ENGINEER)
    phone_number = models.CharField(max_length=20, blank=True)

    # AbstractUser's groups/user_permissions define reverse accessors named
    # "user_set" by default. If this model doesn't fully replace
    # django.contrib.auth's User (i.e. AUTH_USER_MODEL isn't pointed here),
    # or another custom user model exists, those accessors clash.
    # Explicit related_names make this model collision-proof either way.
    groups = models.ManyToManyField(
        "auth.Group",
        verbose_name="groups",
        blank=True,
        help_text="The groups this user belongs to.",
        related_name="project_user_set",
        related_query_name="project_user",
    )
    user_permissions = models.ManyToManyField(
        "auth.Permission",
        verbose_name="user permissions",
        blank=True,
        help_text="Specific permissions for this user.",
        related_name="project_user_set",
        related_query_name="project_user",
    )

    @property
    def is_site_engineer(self):
        return self.role == self.Role.SITE_ENGINEER

    @property
    def is_admin_role(self):
        return self.role == self.Role.ADMIN

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"


# ---------------------------------------------------------------------------
# Phase 1: Client Registration
# ---------------------------------------------------------------------------
# A Customer is the person/company. A Project is the solar job we run for
# them — one customer can have more than one project over time (e.g. a
# residential install now, a commercial extension later), so they're kept
# as separate records from the start rather than bolting fields onto one
# model. Registering a client creates both in a single step.

def _next_sequence(model, field, year):
    """
    Smallest-available-sequence helper shared by Customer/Project ID
    generation. Looks at IDs already issued for the given year and picks
    the next number, rather than counting rows — so a deleted record
    doesn't cause a collision.
    """
    existing = (
        model.objects.filter(**{f"{field}__startswith": f"{model.ID_PREFIX}-{year}-"})
        .values_list(field, flat=True)
    )
    max_n = 0
    for value in existing:
        try:
            n = int(value.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            continue
        max_n = max(max_n, n)
    return max_n + 1


class Customer(models.Model):
    """
    Phase 1 output: Customer record.
    Fields mirror the "Inputs" section of Client Registration in the
    workflow reference: contact details, classification, location,
    referral, and the sales rep who owns the relationship.
    """

    class CustomerType(models.TextChoices):
        RESIDENTIAL = "residential", "Residential"
        COMMERCIAL = "commercial", "Commercial"
        INDUSTRIAL = "industrial", "Industrial"

    ID_PREFIX = "CUS"

    customer_id = models.CharField(max_length=20, unique=True, editable=False)

    # Contact
    full_name = models.CharField("Customer name", max_length=150)
    phone_number = models.CharField(max_length=20)
    email = models.EmailField(blank=True)
    national_id = models.CharField(
        "National ID / company registration number", max_length=50, blank=True
    )

    # Classification
    customer_type = models.CharField(
        max_length=20, choices=CustomerType.choices, default=CustomerType.RESIDENTIAL
    )

    # Location
    county = models.CharField(max_length=100)
    address = models.CharField("Physical address", max_length=255, blank=True)
    gps_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    gps_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    # Sales
    referral_source = models.CharField(max_length=150, blank=True)
    assigned_rep = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_customers",
        help_text="Sales representative who owns this customer relationship.",
    )

    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="registered_customers",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.customer_id:
            year = timezone.now().year
            n = _next_sequence(Customer, "customer_id", year)
            self.customer_id = f"{self.ID_PREFIX}-{year}-{n:04d}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.customer_id} — {self.full_name}"


class Project(models.Model):
    """
    Phase 1 output: Project record, created automatically alongside the
    Customer with status Lead. Later phases (site assessment, quotation,
    installation, ...) advance this same record's status rather than
    creating new objects, so one project ID tracks the full lifecycle.
    """

    class Status(models.TextChoices):
        LEAD = "lead", "Lead"
        SITE_ASSESSMENT = "site_assessment", "Site assessment"
        QUOTATION = "quotation", "Quotation"
        APPROVED = "approved", "Approved"
        INVOICED = "invoiced", "Invoiced"
        IN_PROGRESS = "in_progress", "Installation in progress"
        COMMISSIONED = "commissioned", "Commissioned"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    ID_PREFIX = "PRJ"

    project_id = models.CharField(max_length=20, unique=True, editable=False)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="projects")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.LEAD)
    assigned_rep = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_projects",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.project_id:
            year = timezone.now().year
            n = _next_sequence(Project, "project_id", year)
            self.project_id = f"{self.ID_PREFIX}-{year}-{n:04d}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.project_id} — {self.customer.full_name} ({self.get_status_display()})"


# ---------------------------------------------------------------------------
# Phase 2: Site Assessment
# ---------------------------------------------------------------------------
# Two-step record on the same table: a rep first schedules a visit
# (scheduling fields only), then whoever conducts it comes back and fills
# in the technical survey and marks it complete — which is what advances
# the paired Project into Quotation. One assessment per project for now;
# if the workflow later needs re-assessments this can move to a FK.

class SiteAssessment(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class RoofType(models.TextChoices):
        FLAT = "flat", "Flat"
        PITCHED = "pitched", "Pitched / gable"
        HIP = "hip", "Hip"
        SHED = "shed", "Shed / mono-pitch"
        OTHER = "other", "Other"

    class RoofCondition(models.TextChoices):
        EXCELLENT = "excellent", "Excellent"
        GOOD = "good", "Good"
        FAIR = "fair", "Fair"
        POOR = "poor", "Poor — needs repair"

    class ShadingLevel(models.TextChoices):
        NONE = "none", "None"
        MINIMAL = "minimal", "Minimal"
        MODERATE = "moderate", "Moderate"
        HEAVY = "heavy", "Heavy"

    class Orientation(models.TextChoices):
        N = "n", "North"
        NE = "ne", "North-east"
        E = "e", "East"
        SE = "se", "South-east"
        S = "s", "South"
        SW = "sw", "South-west"
        W = "w", "West"
        NW = "nw", "North-west"

    class SupplyType(models.TextChoices):
        GRID_SINGLE = "grid_single", "Grid — single phase"
        GRID_THREE = "grid_three", "Grid — three phase"
        OFF_GRID = "off_grid", "Off-grid / none"
        GENERATOR = "generator", "Generator backup"

    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name="site_assessment"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)

    # --- Scheduling (Step 1) ---
    scheduled_date = models.DateTimeField()
    assigned_engineer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="site_assessments",
        help_text="Site engineer who will conduct the visit.",
    )
    scheduling_notes = models.TextField(blank=True)
    scheduled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assessments_booked",
    )
    scheduled_at = models.DateTimeField(default=timezone.now)

    # --- Technical survey (Step 2) ---
    roof_type = models.CharField(max_length=20, choices=RoofType.choices, blank=True)
    roof_material = models.CharField(max_length=100, blank=True)
    roof_area_sqm = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    roof_orientation = models.CharField(max_length=4, choices=Orientation.choices, blank=True)
    roof_tilt_degrees = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    roof_condition = models.CharField(max_length=20, choices=RoofCondition.choices, blank=True)
    structural_integrity_ok = models.BooleanField(null=True, blank=True)
    structural_notes = models.TextField(blank=True)

    shading_level = models.CharField(max_length=20, choices=ShadingLevel.choices, blank=True)
    shading_notes = models.TextField(blank=True)

    current_supply_type = models.CharField(max_length=20, choices=SupplyType.choices, blank=True)
    electrical_panel_capacity_amps = models.PositiveIntegerField(null=True, blank=True)
    average_monthly_consumption_kwh = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    has_existing_backup = models.BooleanField(default=False)
    backup_notes = models.TextField(blank=True)

    available_installation_area_sqm = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    roof_access_notes = models.TextField(blank=True)

    # Assessor's own call — always wins over the auto-calculated
    # SystemRecommendation below. The calculator is an aid, not a replacement
    # for someone who was actually on the roof.
    recommended_system_size_kw = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    assessor_notes = models.TextField(blank=True)

    assessed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assessments_conducted",
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-scheduled_date"]

    @property
    def is_completed(self):
        return self.status == self.Status.COMPLETED

    def __str__(self):
        return f"Site assessment — {self.project.project_id} ({self.get_status_display()})"


# ---------------------------------------------------------------------------
# Equipment catalog
# ---------------------------------------------------------------------------
# What's actually in stock, so the sizing calculator (see sizing.py) has
# real items to recommend rather than a bare kW number. Three separate
# models rather than one generic "Equipment" table, because panels,
# inverters and batteries are specified in genuinely different units
# (watts vs kW vs kWh) and a shared table would need a pile of nullable
# fields that only apply to one type.

class EquipmentBase(models.Model):
    name = models.CharField(max_length=150, help_text="e.g. 'Mono PERC 450W' or 'Growatt SPF 5000ES'")
    brand = models.CharField(max_length=100, blank=True)
    model_number = models.CharField(max_length=100, blank=True)
    unit_price_kes = models.DecimalField(
        "Unit price (KES)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    stock_quantity = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(
        default=True, help_text="Inactive items are hidden from the sizing calculator and new quotations."
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def __str__(self):
        label = f"{self.brand} {self.name}".strip()
        return label or f"{self._meta.verbose_name} #{self.pk}"

    @property
    def in_stock(self):
        return self.is_active and self.stock_quantity > 0


class SolarPanel(EquipmentBase):
    wattage_w = models.PositiveIntegerField("Wattage (W)", help_text="Rated power per panel")
    panel_area_sqm = models.DecimalField(
        "Panel footprint (m²)", max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Physical area of one panel — used to check it will fit the roof.",
    )
    efficiency_percent = models.DecimalField(
        max_digits=4, decimal_places=1, null=True, blank=True
    )

    class Meta:
        verbose_name = "Solar panel"
        ordering = ["wattage_w", "brand"]

    def __str__(self):
        return f"{super().__str__()} — {self.wattage_w}W"


class Inverter(EquipmentBase):
    class Phase(models.TextChoices):
        SINGLE = "single", "Single phase"
        THREE = "three", "Three phase"

    capacity_kw = models.DecimalField(
        "Rated output (kW)", max_digits=6, decimal_places=2
    )
    phase = models.CharField(max_length=10, choices=Phase.choices, default=Phase.SINGLE)
    is_hybrid = models.BooleanField(
        default=True, help_text="Can charge/discharge a battery bank, not grid-tie only."
    )
    max_input_voltage = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        verbose_name = "Inverter"
        ordering = ["capacity_kw", "brand"]

    def __str__(self):
        return f"{super().__str__()} — {self.capacity_kw}kW {self.get_phase_display()}"


class Battery(EquipmentBase):
    class Chemistry(models.TextChoices):
        LITHIUM = "lithium", "Lithium (LiFePO4)"
        LEAD_ACID = "lead_acid", "Lead-acid"

    capacity_kwh = models.DecimalField(
        "Capacity (kWh)", max_digits=6, decimal_places=2
    )
    chemistry = models.CharField(max_length=20, choices=Chemistry.choices, default=Chemistry.LITHIUM)
    usable_depth_of_discharge_percent = models.PositiveIntegerField(
        "Usable DoD (%)", default=90,
        help_text="How much of nameplate capacity is safely usable — typically ~90% for lithium, ~50% for lead-acid.",
    )
    max_discharge_rate_kw = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )

    class Meta:
        verbose_name = "Battery"
        verbose_name_plural = "Batteries"
        ordering = ["capacity_kwh", "brand"]

    def __str__(self):
        return f"{super().__str__()} — {self.capacity_kwh}kWh"


# ---------------------------------------------------------------------------
# System recommendation
# ---------------------------------------------------------------------------
# The output of the rule-based sizing calculator (sizing.py), kept as its
# own record — separate from SiteAssessment.recommended_system_size_kw,
# which is the assessor's own manual figure. This one is machine-generated,
# recalculable, and can be regenerated any time the survey or catalog
# stock changes without touching the assessor's own entry.

class SystemRecommendation(models.Model):
    site_assessment = models.OneToOneField(
        SiteAssessment, on_delete=models.CASCADE, related_name="recommendation"
    )

    # Calculated targets
    daily_consumption_kwh = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    target_system_size_kw = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    target_battery_capacity_kwh = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    backup_autonomy_hours = models.PositiveIntegerField(default=0)

    # Matched catalog picks (nullable — calculation may not find a fit)
    panel = models.ForeignKey(SolarPanel, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    panel_quantity = models.PositiveIntegerField(default=0)
    inverter = models.ForeignKey(Inverter, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    battery = models.ForeignKey(Battery, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    battery_quantity = models.PositiveIntegerField(default=0)

    warnings = models.JSONField(default=list, blank=True)
    calculated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Recommendation — {self.site_assessment.project.project_id}"

    @property
    def estimated_total_cost_kes(self):
        total = 0
        has_price = False
        for item, qty in (
            (self.panel, self.panel_quantity),
            (self.inverter, 1 if self.inverter else 0),
            (self.battery, self.battery_quantity),
        ):
            if item and item.unit_price_kes is not None:
                total += item.unit_price_kes * qty
                has_price = True
        return total if has_price else None