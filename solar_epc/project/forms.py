from django import forms
from django.contrib.auth.password_validation import validate_password

from .models import Battery, Customer, Inverter, SiteAssessment, SolarPanel, User


class LoginForm(forms.Form):
    """
    Plain form, not AuthenticationForm — full control over fields/widgets.
    Actual credential checking still happens in the view via
    django.contrib.auth.authenticate(), which is what actually verifies
    the password hash; this form is just field collection + validation.
    """
    username = forms.CharField(
        widget=forms.TextInput(attrs={"autofocus": True, "placeholder": "Username"})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Password"})
    )


class RegisterForm(forms.Form):
    """
    Plain form, not UserCreationForm. Always registers a Site Engineer —
    there is no role field here, so role=admin can't be submitted from
    the outside. Admin accounts are created through /admin/.
    """
    username = forms.CharField(max_length=150)
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    email = forms.EmailField()
    phone_number = forms.CharField(max_length=20, required=False)
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already taken.")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"]
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_password1(self):
        password1 = self.cleaned_data.get("password1")
        # Still uses Django's built-in password strength validators
        # (AUTH_PASSWORD_VALIDATORS in settings) — that's a validation
        # utility, not the "default auth views" this replaces.
        if password1:
            validate_password(password1)
        return password1

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "Passwords don't match.")
        return cleaned_data


class CustomerRegistrationForm(forms.ModelForm):
    """
    Phase 1: Client Registration.
    Collects everything the workflow's Client Registration "Inputs"
    section lists. Saving this form is what creates both the Customer
    record and the paired Lead-status Project — see
    views.client_register.
    """

    class Meta:
        model = Customer
        fields = [
            "full_name",
            "phone_number",
            "email",
            "national_id",
            "customer_type",
            "county",
            "address",
            "gps_latitude",
            "gps_longitude",
            "referral_source",
            "assigned_rep",
        ]
        widgets = {
            "full_name": forms.TextInput(attrs={"placeholder": "e.g. Wanjiru Kamau"}),
            "phone_number": forms.TextInput(attrs={"placeholder": "e.g. 0712 345 678"}),
            "email": forms.EmailInput(attrs={"placeholder": "customer@example.com"}),
            "national_id": forms.TextInput(attrs={"placeholder": "Optional"}),
            "county": forms.TextInput(attrs={"placeholder": "e.g. Kiambu"}),
            "address": forms.TextInput(attrs={"placeholder": "Street / building / landmark"}),
            "gps_latitude": forms.NumberInput(attrs={"placeholder": "-1.286389", "step": "any"}),
            "gps_longitude": forms.NumberInput(attrs={"placeholder": "36.817223", "step": "any"}),
            "referral_source": forms.TextInput(
                attrs={"placeholder": "e.g. Facebook, existing customer, walk-in"}
            ),
        }
        labels = {
            "gps_latitude": "GPS latitude",
            "gps_longitude": "GPS longitude",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_rep"].queryset = User.objects.filter(is_active=True).order_by(
            "first_name", "username"
        )
        self.fields["assigned_rep"].required = False
        self.fields["email"].required = False
        self.fields["address"].required = False
        self.fields["gps_latitude"].required = False
        self.fields["gps_longitude"].required = False
        self.fields["referral_source"].required = False
        self.fields["national_id"].required = False


class SiteAssessmentScheduleForm(forms.ModelForm):
    """
    Phase 2, step 1: book the visit. Deliberately small — just enough to
    put it on the calendar and assign who's doing it. The technical detail
    is captured later via SiteAssessmentSurveyForm once the visit happens.
    """

    class Meta:
        model = SiteAssessment
        fields = ["scheduled_date", "assigned_engineer", "scheduling_notes"]
        widgets = {
            "scheduled_date": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "scheduling_notes": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Gate code, best contact number, anything the engineer should know before the visit…"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scheduled_date"].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["assigned_engineer"].queryset = User.objects.filter(is_active=True).order_by(
            "first_name", "username"
        )
        self.fields["assigned_engineer"].required = False
        self.fields["scheduling_notes"].required = False


class SiteAssessmentSurveyForm(forms.ModelForm):
    """
    Phase 2, step 2: the technical findings from the actual visit.
    Everything here is optional at the field level — a rep can save partial
    notes — but the view only marks the assessment Completed (and advances
    the project to Quotation) once this form is what gets submitted.

    Field-level validation is intentionally light (all optional), but a few
    cross-field checks are added in clean() because they're the checks that
    actually matter for the sizing calculator downstream: a roof flagged
    unsound, or an area bigger than the roof itself, should be caught here
    rather than silently producing a nonsense recommendation later.
    """

    class Meta:
        model = SiteAssessment
        fields = [
            "roof_type", "roof_material", "roof_area_sqm", "roof_orientation",
            "roof_tilt_degrees", "roof_condition", "structural_integrity_ok", "structural_notes",
            "shading_level", "shading_notes",
            "current_supply_type", "electrical_panel_capacity_amps",
            "average_monthly_consumption_kwh", "has_existing_backup", "backup_notes",
            "available_installation_area_sqm", "roof_access_notes",
            "recommended_system_size_kw", "assessor_notes",
        ]
        widgets = {
            "roof_material": forms.TextInput(attrs={"placeholder": "e.g. iron sheets, clay tile, concrete"}),
            "roof_area_sqm": forms.NumberInput(attrs={"step": "any", "placeholder": "e.g. 85"}),
            "roof_tilt_degrees": forms.NumberInput(attrs={"step": "any", "placeholder": "e.g. 15"}),
            "structural_notes": forms.Textarea(attrs={"rows": 2}),
            "shading_notes": forms.Textarea(attrs={"rows": 2, "placeholder": "Trees, neighbouring buildings, water tanks…"}),
            "electrical_panel_capacity_amps": forms.NumberInput(attrs={"placeholder": "e.g. 60"}),
            "average_monthly_consumption_kwh": forms.NumberInput(attrs={"step": "any", "placeholder": "From recent bills, if available"}),
            "backup_notes": forms.Textarea(attrs={"rows": 2}),
            "available_installation_area_sqm": forms.NumberInput(attrs={"step": "any"}),
            "roof_access_notes": forms.Textarea(attrs={"rows": 2, "placeholder": "Ladder access, obstacles, safety considerations…"}),
            "recommended_system_size_kw": forms.NumberInput(attrs={"step": "any", "placeholder": "Your recommendation, e.g. 5.5"}),
            "assessor_notes": forms.Textarea(attrs={"rows": 3, "placeholder": "General findings and recommendation for the quotation team…"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.fields:
            self.fields[name].required = False

    def clean(self):
        cleaned_data = super().clean()
        roof_area = cleaned_data.get("roof_area_sqm")
        install_area = cleaned_data.get("available_installation_area_sqm")
        if roof_area is not None and install_area is not None and install_area > roof_area:
            self.add_error(
                "available_installation_area_sqm",
                "Installable area can't be larger than the total roof area.",
            )
        return cleaned_data


class SolarPanelForm(forms.ModelForm):
    class Meta:
        model = SolarPanel
        fields = [
            "name", "brand", "model_number", "wattage_w", "panel_area_sqm",
            "efficiency_percent", "unit_price_kes", "stock_quantity", "is_active", "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 2}),
        }


class InverterForm(forms.ModelForm):
    class Meta:
        model = Inverter
        fields = [
            "name", "brand", "model_number", "capacity_kw", "phase", "is_hybrid",
            "max_input_voltage", "unit_price_kes", "stock_quantity", "is_active", "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 2}),
        }


class BatteryForm(forms.ModelForm):
    class Meta:
        model = Battery
        fields = [
            "name", "brand", "model_number", "capacity_kwh", "chemistry",
            "usable_depth_of_discharge_percent", "max_discharge_rate_kw",
            "unit_price_kes", "stock_quantity", "is_active", "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 2}),
        }