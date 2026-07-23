from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    BatteryForm,
    CustomerRegistrationForm,
    InverterForm,
    LoginForm,
    RegisterForm,
    SiteAssessmentScheduleForm,
    SiteAssessmentSurveyForm,
    SolarPanelForm,
)
from .models import (
    Battery,
    Customer,
    Inverter,
    Project,
    SiteAssessment,
    SolarPanel,
    User,
)
from .sizing import calculate_recommendation


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = LoginForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        user = authenticate(
            request,
            username=form.cleaned_data["username"],
            password=form.cleaned_data["password"],
        )
        if user is not None:
            login(request, user)
            next_url = request.POST.get("next") or request.GET.get("next")
            return redirect(next_url or "dashboard")
        # Attached to the form for this render only — not to the session
        # via messages.error(), which would otherwise persist and show up
        # on the next page the user lands on (e.g. the dashboard right
        # after a subsequent successful login).
        form.add_error(None, "Incorrect username or password.")

    return render(request, "project/login.html", {"form": form})


def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = RegisterForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        user = User.objects.create_user(
            username=form.cleaned_data["username"],
            email=form.cleaned_data["email"],
            first_name=form.cleaned_data["first_name"],
            last_name=form.cleaned_data["last_name"],
            password=form.cleaned_data["password1"],
        )
        user.phone_number = form.cleaned_data["phone_number"]
        user.role = User.Role.SITE_ENGINEER
        user.save()

        login(request, user)
        messages.success(request, f"Welcome, {user.get_full_name() or user.username}.")
        return redirect("dashboard")

    return render(request, "project/register.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def dashboard(request):
    """
    Single entry point after login. Renders a different template per role
    rather than redirecting to /admin/ or /engineer/ URLs, so the URL
    stays stable (bookmarkable) regardless of who's logged in.
    """
    customers = Customer.objects.all()
    projects = Project.objects.all()
    if request.user.is_site_engineer:
        # Site engineers see their own book of business; admins see all.
        customers = customers.filter(assigned_rep=request.user)
        projects = projects.filter(assigned_rep=request.user)

    today = timezone.now()
    context = {
        "total_customers": customers.count(),
        "leads_this_month": projects.filter(
            status=Project.Status.LEAD,
            created_at__year=today.year,
            created_at__month=today.month,
        ).count(),
        "active_projects": projects.exclude(
            status__in=[Project.Status.COMPLETED, Project.Status.CANCELLED]
        ).count(),
        "recent_customers": customers.order_by("-created_at")[:5],
    }

    if request.user.is_admin_role:
        catalog_total_active, low_stock_items = _catalog_overview()
        context["catalog_total_active"] = catalog_total_active
        context["low_stock_items"] = low_stock_items

    return render(request, "project/dashboard.html", context)


@login_required
def client_list(request):
    """Phase 1: browse registered customers and jump into a new registration."""
    customers = Customer.objects.select_related("assigned_rep").prefetch_related("projects")
    if request.user.is_site_engineer:
        customers = customers.filter(assigned_rep=request.user)

    query = request.GET.get("q", "").strip()
    if query:
        customers = customers.filter(
            Q(full_name__icontains=query)
            | Q(customer_id__icontains=query)
            | Q(phone_number__icontains=query)
            | Q(email__icontains=query)
        )

    return render(
        request,
        "project/client_list.html",
        {"customers": customers, "query": query},
    )


@login_required
def client_register(request):
    """
    Phase 1: Client Registration.
    Creates the Customer record and, in the same transaction, a paired
    Project with status Lead — matching the workflow's System Actions
    for this phase (customer profile + new Lead project together).
    """
    form = CustomerRegistrationForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            customer = form.save(commit=False)
            customer.registered_by = request.user
            if not customer.assigned_rep_id and request.user.is_site_engineer:
                customer.assigned_rep = request.user
            customer.save()

            Project.objects.create(
                customer=customer,
                assigned_rep=customer.assigned_rep,
            )

        messages.success(
            request, f"{customer.full_name} registered as {customer.customer_id}."
        )
        return redirect("client_detail", customer_id=customer.customer_id)

    return render(request, "project/client_form.html", {"form": form})


@login_required
def client_detail(request, customer_id):
    """Phase 1 output: customer profile plus the project(s) it kicked off."""
    customer = get_object_or_404(Customer, customer_id=customer_id)
    if request.user.is_site_engineer and customer.assigned_rep_id != request.user.id:
        messages.error(request, "That client isn't assigned to you.")
        return redirect("client_list")

    return render(
        request,
        "project/client_detail.html",
        {"customer": customer, "projects": customer.projects.all()},
    )


def _get_customer_for_user(request, customer_id):
    """
    Shared lookup + access check used by every Phase 2 view: site engineers
    can only act on their own book of business, same rule as client_detail.
    Returns (customer, redirect_response). redirect_response is None if
    access is allowed.
    """
    customer = get_object_or_404(Customer, customer_id=customer_id)
    if request.user.is_site_engineer and customer.assigned_rep_id != request.user.id:
        messages.error(request, "That client isn't assigned to you.")
        return customer, redirect("client_list")
    return customer, None


@login_required
def site_assessment_schedule(request, customer_id):
    """
    Phase 2, step 1: book (or reschedule) the site visit. Creates the
    SiteAssessment record on first use, and nudges the project's status
    into Site assessment if it's still sitting at Lead.
    """
    customer, denied = _get_customer_for_user(request, customer_id)
    if denied:
        return denied

    project = customer.projects.order_by("-created_at").first()
    if project is None:
        messages.error(request, "This client has no project to schedule an assessment for.")
        return redirect("client_detail", customer_id=customer.customer_id)

    assessment = getattr(project, "site_assessment", None)
    form = SiteAssessmentScheduleForm(request.POST or None, instance=assessment)

    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            assessment = form.save(commit=False)
            assessment.project = project
            if assessment.pk is None:
                assessment.scheduled_by = request.user
            assessment.status = SiteAssessment.Status.SCHEDULED
            assessment.save()

            if project.status == Project.Status.LEAD:
                project.status = Project.Status.SITE_ASSESSMENT
                project.save(update_fields=["status"])

        messages.success(
            request, f"Site assessment scheduled for {project.project_id}."
        )
        return redirect("client_detail", customer_id=customer.customer_id)

    return render(
        request,
        "project/site_assessment_form.html",
        {"form": form, "customer": customer, "project": project, "assessment": assessment},
    )


@login_required
def site_assessment_survey(request, customer_id):
    """
    Phase 2, step 2: record what was actually found on-site. Submitting
    this form is what marks the assessment Completed and advances the
    project into Quotation — matching the workflow's System Actions for
    this phase.

    On every successful save (not just on completion) the rule-based sizing
    calculator re-runs against whatever's been entered so far and refreshes
    this assessment's SystemRecommendation — see sizing.py. That keeps the
    catalog-matched recommendation live as the survey is filled in, rather
    than a one-shot calculation only at the very end.
    """
    customer, denied = _get_customer_for_user(request, customer_id)
    if denied:
        return denied

    project = customer.projects.order_by("-created_at").first()
    assessment = get_object_or_404(SiteAssessment, project=project)

    if assessment.status == SiteAssessment.Status.COMPLETED:
        messages.info(request, "This site assessment has already been completed.")

    form = SiteAssessmentSurveyForm(request.POST or None, instance=assessment)

    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            assessment = form.save(commit=False)
            assessment.status = SiteAssessment.Status.COMPLETED
            assessment.assessed_by = request.user
            assessment.completed_at = timezone.now()
            assessment.save()

            project.status = Project.Status.QUOTATION
            project.save(update_fields=["status"])

        calculate_recommendation(assessment)

        messages.success(
            request,
            f"Site assessment completed. {project.project_id} moved to Quotation.",
        )
        return redirect(
            "site_assessment_recommendation", customer_id=customer.customer_id
        )

    return render(
        request,
        "project/site_assessment_survey.html",
        {"form": form, "customer": customer, "project": project, "assessment": assessment},
    )


@login_required
def site_assessment_recommendation(request, customer_id):
    """
    Shows the sizing calculator's output for this assessment: target system
    size, target battery capacity, and the best-fit catalog items for each,
    with a manual "Recalculate" action for when the catalog changes after
    the survey was completed.
    """
    customer, denied = _get_customer_for_user(request, customer_id)
    if denied:
        return denied

    project = customer.projects.order_by("-created_at").first()
    assessment = get_object_or_404(SiteAssessment, project=project)

    if request.method == "POST":
        calculate_recommendation(assessment)
        messages.success(request, "Recommendation recalculated against current stock.")
        return redirect("site_assessment_recommendation", customer_id=customer.customer_id)

    recommendation = getattr(assessment, "recommendation", None)
    if recommendation is None:
        recommendation = calculate_recommendation(assessment)

    return render(
        request,
        "project/site_assessment_recommendation.html",
        {
            "customer": customer,
            "project": project,
            "assessment": assessment,
            "recommendation": recommendation,
        },
    )


# ---------------------------------------------------------------------------
# Equipment catalog
# ---------------------------------------------------------------------------
# One small set of generic views driven by a registry, rather than three
# near-identical copies of list/form/delete — panels, inverters and
# batteries differ only in model/form/labels, not in CRUD behaviour.

CATALOG_REGISTRY = {
    "panels": {
        "model": SolarPanel,
        "form": SolarPanelForm,
        "label": "Solar panels",
        "singular": "panel",
    },
    "inverters": {
        "model": Inverter,
        "form": InverterForm,
        "label": "Inverters",
        "singular": "inverter",
    },
    "batteries": {
        "model": Battery,
        "form": BatteryForm,
        "label": "Batteries",
        "singular": "battery",
    },
}

LOW_STOCK_THRESHOLD = 3


def _catalog_overview():
    """
    Used by the dashboard: total active catalog items, and the active items
    running low on stock (used to size the sizing calculator's matches) so
    an admin can restock before it starts failing to find a fit.
    """
    total_active = 0
    low_stock = []
    for kind, entry in CATALOG_REGISTRY.items():
        active_items = entry["model"].objects.filter(is_active=True)
        total_active += active_items.count()
        for item in active_items.filter(stock_quantity__lte=LOW_STOCK_THRESHOLD).order_by("stock_quantity"):
            low_stock.append({"kind": kind, "singular": entry["singular"], "item": item})
    low_stock.sort(key=lambda row: row["item"].stock_quantity)
    return total_active, low_stock


def _catalog_entry(kind):
    entry = CATALOG_REGISTRY.get(kind)
    if entry is None:
        raise Http404("Unknown equipment type.")
    return entry


@login_required
def catalog_home(request):
    """Landing page for equipment catalog management, admins only."""
    if not request.user.is_admin_role:
        messages.error(request, "Only admins can manage the equipment catalog.")
        return redirect("dashboard")

    catalog_cards = [
        {"kind": kind, "entry": entry, "count": entry["model"].objects.count()}
        for kind, entry in CATALOG_REGISTRY.items()
    ]
    return render(
        request,
        "project/catalog_home.html",
        {"catalog_cards": catalog_cards},
    )


@login_required
def equipment_list(request, kind):
    if not request.user.is_admin_role:
        messages.error(request, "Only admins can manage the equipment catalog.")
        return redirect("dashboard")

    entry = _catalog_entry(kind)
    items = entry["model"].objects.all()

    query = request.GET.get("q", "").strip()
    if query:
        items = items.filter(Q(name__icontains=query) | Q(brand__icontains=query))

    return render(
        request,
        "project/equipment_list.html",
        {"kind": kind, "entry": entry, "items": items, "query": query},
    )


@login_required
def equipment_form(request, kind, pk=None):
    if not request.user.is_admin_role:
        messages.error(request, "Only admins can manage the equipment catalog.")
        return redirect("dashboard")

    entry = _catalog_entry(kind)
    instance = get_object_or_404(entry["model"], pk=pk) if pk else None
    form = entry["form"](request.POST or None, instance=instance)

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(
            request,
            f"{entry['singular'].capitalize()} {'updated' if pk else 'added'}.",
        )
        return redirect("equipment_list", kind=kind)

    return render(
        request,
        "project/equipment_form.html",
        {"kind": kind, "entry": entry, "form": form, "instance": instance},
    )


@login_required
@require_POST
def equipment_delete(request, kind, pk):
    if not request.user.is_admin_role:
        messages.error(request, "Only admins can manage the equipment catalog.")
        return redirect("dashboard")

    entry = _catalog_entry(kind)
    item = get_object_or_404(entry["model"], pk=pk)
    item.delete()
    messages.success(request, f"{entry['singular'].capitalize()} removed from the catalog.")
    return redirect("equipment_list", kind=kind)