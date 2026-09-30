import io

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .forms import (
    BatteryForm,
    CustomerRegistrationForm,
    InverterForm,
    LoginForm,
    ProductCategoryForm,
    ProductForm,
    QuotationForm,
    RegisterForm,
    SiteAssessmentScheduleForm,
    SiteAssessmentSurveyForm,
    SolarPanelForm,
    SystemRecommendationReviewForm,
    quotation_line_item_formset,
    site_load_formset,
)
from .models import (
    Battery,
    Customer,
    Inverter,
    Product,
    ProductCategory,
    Project,
    Quotation,
    QuotationLineItem,
    SiteAssessment,
    SolarPanel,
    User,
)
from .dashboard_metrics import build_dashboard_data
from .sizing import calculate_recommendation


def quotations_for_user(user):
    """
    Every quotation the user may see, newest first.

    Adds what quotation_list relies on:
      - latest_project_id: the customer's most recent project, so the view can
        tell whether a quotation is on that latest project (openable) or an older one.
      - total_kes: the quotation total (currently the line-item subtotal).
    Site engineers only see quotations for their own clients.
    """
    latest_project = (
        Project.objects
        .filter(customer=OuterRef("project__customer"))
        .order_by("-created_at")
        .values("id")[:1]
    )
    qs = (
        Quotation.objects
        .select_related("project", "project__customer")
        .prefetch_related("line_items")
        .annotate(latest_project_id=Subquery(latest_project))
        .order_by("-created_at")
    )
    if user.is_site_engineer:
        qs = qs.filter(project__customer__assigned_rep=user)

    rows = list(qs)
    for q in rows:
        q.total_kes = q.subtotal_kes
    return rows


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


def _dashboard_payload(user):
    """Role-scoped dashboard metrics; shared by the page and its live refresh."""
    catalog_total = _catalog_overview()[0] if user.is_admin_role else None
    return build_dashboard_data(user, catalog_total_active=catalog_total)


@login_required
def dashboard(request):
    """
    Single entry point after login. Renders a different template per role
    rather than redirecting to /admin/ or /engineer/ URLs, so the URL
    stays stable (bookmarkable) regardless of who's logged in.

    The metrics are embedded in the page for an instant first paint; the
    page then refreshes them from dashboard_data every 30 seconds.
    """
    return render(
        request,
        "project/dashboard.html",
        {"dashboard_data": _dashboard_payload(request.user)},
    )


@login_required
@never_cache
def dashboard_data(request):
    """JSON feed behind the dashboard's live refresh (same scoping as the page)."""
    return JsonResponse(_dashboard_payload(request.user))


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

    # The Phase 2+ views all operate on the customer's most recent project
    # (customer.projects.order_by("-created_at").first()), so only that
    # project gets working workflow links; older ones are shown read-only.
    latest = customer.projects.order_by("-created_at").first()
    project_rows = [
        {
            "project": project,
            "assessment": getattr(project, "site_assessment", None),
            "quotation": getattr(project, "quotation", None),
            "actions": _project_actions(project),
            "is_latest": latest is not None and project.pk == latest.pk,
            "workflow": _project_workflow(project),
        }
        for project in customer.projects.all()
    ]

    return render(
        request,
        "project/client_detail.html",
        {"customer": customer, "project_rows": project_rows},
    )


def _project_actions(project):
    """
    What to show the rep next for a given project, based on how far it's
    got through the workflow. Kept as one function so client_detail.html
    doesn't need to encode phase-sequencing logic itself.
    """
    assessment = getattr(project, "site_assessment", None)
    quotation = getattr(project, "quotation", None)

    if assessment is None:
        return [{"label": "Schedule site assessment", "url_name": "site_assessment_schedule", "primary": True}]

    if assessment.status != SiteAssessment.Status.COMPLETED:
        return [
            {"label": "Continue survey", "url_name": "site_assessment_survey", "primary": True},
            {"label": "Reschedule visit", "url_name": "site_assessment_schedule", "primary": False},
        ]

    if quotation is None:
        recommendation = getattr(assessment, "recommendation", None)
        if recommendation is None or not recommendation.is_reviewed:
            return [{"label": "Review recommendation", "url_name": "site_assessment_recommendation", "primary": True}]
        return [
            {"label": "View recommendation", "url_name": "site_assessment_recommendation", "primary": False},
            {"label": "Create quotation", "url_name": "quotation_edit", "primary": True},
        ]

    actions = [{"label": "View quotation", "url_name": "quotation_detail", "primary": True}]
    if quotation.status in (Quotation.Status.DRAFT, Quotation.Status.REJECTED):
        actions.append({"label": "Edit quotation", "url_name": "quotation_edit", "primary": False})
    return actions


def _project_workflow(project):
    """
    Every step of the site-assessment -> quotation pipeline for a project,
    with its state and the links that are valid for it *right now*.

    Unlike _project_actions (which only surfaces the single next step), this
    keeps earlier steps reachable so a rep can go back and add or amend
    something without hunting for a URL. Steps whose prerequisites aren't
    met are returned as "locked" with no links, mirroring the guards the
    views themselves enforce.

    step["state"] is one of: done | current | todo | locked
    (the first unlocked, unfinished step is promoted to "current").
    """
    assessment = getattr(project, "site_assessment", None)
    recommendation = getattr(assessment, "recommendation", None) if assessment else None
    quotation = getattr(project, "quotation", None)

    has_assessment = assessment is not None
    survey_done = has_assessment and assessment.status == SiteAssessment.Status.COMPLETED
    rec_reviewed = bool(recommendation and recommendation.is_reviewed)

    # 1. Site visit -----------------------------------------------------
    if not has_assessment:
        visit = {"state": "todo", "summary": "Book a date to inspect the site.",
                 "links": [{"label": "Schedule visit", "url_name": "site_assessment_schedule"}]}
    elif not survey_done:
        visit = {"state": "done", "summary": "Visit scheduled.",
                 "links": [{"label": "Reschedule visit", "url_name": "site_assessment_schedule"}]}
    else:
        # Rescheduling a completed assessment would knock it back to
        # "Scheduled", so no link once the survey has been submitted.
        visit = {"state": "done", "summary": "Visit completed.", "links": []}
    visit.update(key="visit", title="Site visit", date=None)

    # 2. Survey ---------------------------------------------------------
    if not has_assessment:
        survey = {"state": "locked", "summary": "Unlocks once a site visit is scheduled.", "links": [], "date": None}
    elif not survey_done:
        survey = {"state": "todo", "summary": "Record what was found on-site.", "date": None,
                  "links": [{"label": "Continue survey", "url_name": "site_assessment_survey"}]}
    else:
        survey = {"state": "done", "summary": "Survey completed.", "date": assessment.completed_at,
                  "links": [{"label": "View / edit survey", "url_name": "site_assessment_survey"}]}
    survey.update(key="survey", title="Site survey")

    # 3. Recommendation -------------------------------------------------
    if not survey_done:
        rec = {"state": "locked", "summary": "Unlocks after the survey is completed.", "links": [], "date": None}
    elif not rec_reviewed:
        rec = {"state": "todo", "summary": "Waiting for engineer sign-off.", "date": None,
               "links": [{"label": "Review recommendation", "url_name": "site_assessment_recommendation"}]}
    else:
        rec = {"state": "done", "summary": "Approved by engineer.", "date": recommendation.reviewed_at,
               "links": [{"label": "View recommendation", "url_name": "site_assessment_recommendation"}]}
    rec.update(key="recommendation", title="System recommendation")

    # 4. Quotation ------------------------------------------------------
    if quotation is not None:
        links = [{"label": "View quotation", "url_name": "quotation_detail"}]
        if quotation.status in (Quotation.Status.DRAFT, Quotation.Status.REJECTED):
            links.append({"label": "Edit quotation", "url_name": "quotation_edit"})
        quote = {"state": "done", "date": None, "links": links,
                 "summary": f"{quotation.quotation_id} \u00b7 {quotation.get_status_display()}"}
    elif rec_reviewed:
        quote = {"state": "todo", "summary": "Ready to be created.", "date": None,
                 "links": [{"label": "Create quotation", "url_name": "quotation_edit"}]}
    else:
        quote = {"state": "locked", "summary": "Unlocks after the recommendation is approved.", "links": [], "date": None}
    quote.update(key="quotation", title="Quotation")

    steps = [visit, survey, rec, quote]
    for number, step in enumerate(steps, start=1):
        step["number"] = number
    for step in steps:
        if step["state"] == "todo":
            step["state"] = "current"
            break
    return steps


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
    # Devices the customer needs powered. Suggestions are pre-filled from the
    # customer's classification (residential / commercial / industrial).
    load_formset = site_load_formset(assessment, request.POST or None)

    if request.method == "POST" and form.is_valid() and load_formset.is_valid():
        with transaction.atomic():
            assessment = form.save(commit=False)
            assessment.status = SiteAssessment.Status.COMPLETED
            assessment.assessed_by = request.user
            assessment.completed_at = timezone.now()
            assessment.save()

            load_formset.instance = assessment
            load_formset.save()

            project.status = Project.Status.ENGINEER_REVIEW
            project.save(update_fields=["status"])

        calculate_recommendation(assessment)

        messages.success(
            request,
            f"Site assessment completed. {project.project_id} moved to Engineer review.",
        )
        return redirect(
            "site_assessment_recommendation", customer_id=customer.customer_id
        )

    return render(
        request,
        "project/site_assessment_survey.html",
        {
            "form": form,
            "load_formset": load_formset,
            "customer": customer,
            "project": project,
            "assessment": assessment,
        },
    )


@login_required
def site_assessment_recommendation(request, customer_id):
    """
    Phase 6: Engineer review. Shows the sizing calculator's output for this
    assessment — target system size, target battery capacity, best-fit
    catalog items — as an editable form: an engineer can adjust the
    matched equipment/quantities, then either just save the adjustment or
    save-and-sign-off, which is what's required before Quotation unlocks
    (see quotation_edit and _project_actions).

    Three POST actions share this one view/template rather than being
    split into separate pages, since they're all "look at this
    recommendation and act on it" and the reviewer shouldn't have to
    navigate anywhere to do any of them:
      - recalculate: reruns the calculator from scratch against current
        catalog, discarding any unsaved edits below (existing behaviour).
      - save: persists edits to the pick/quantities/notes without signing off.
      - approve: same save, plus marks is_reviewed and advances the project
        to Quotation.
    """
    customer, denied = _get_customer_for_user(request, customer_id)
    if denied:
        return denied

    project = customer.projects.order_by("-created_at").first()
    assessment = get_object_or_404(SiteAssessment, project=project)

    recommendation = getattr(assessment, "recommendation", None)
    if recommendation is None:
        recommendation = calculate_recommendation(assessment)

    if request.method == "POST" and "recalculate" in request.POST:
        calculate_recommendation(assessment)
        messages.success(request, "Recommendation recalculated against the current catalog.")
        return redirect("site_assessment_recommendation", customer_id=customer.customer_id)

    if request.method == "POST":
        form = SystemRecommendationReviewForm(request.POST, instance=recommendation)
        if form.is_valid():
            recommendation = form.save(commit=False)
            approving = "approve" in request.POST
            if approving:
                recommendation.is_reviewed = True
                recommendation.reviewed_by = request.user
                recommendation.reviewed_at = timezone.now()
            recommendation.save()

            if approving:
                if project.status == Project.Status.ENGINEER_REVIEW:
                    project.status = Project.Status.QUOTATION
                    project.save(update_fields=["status"])
                messages.success(
                    request, f"Recommendation approved. {project.project_id} moved to Quotation."
                )
                return redirect("client_detail", customer_id=customer.customer_id)

            messages.success(request, "Review notes saved.")
            return redirect("site_assessment_recommendation", customer_id=customer.customer_id)
    else:
        form = SystemRecommendationReviewForm(instance=recommendation)

    return render(
        request,
        "project/site_assessment_recommendation.html",
        {
            "customer": customer,
            "project": project,
            "assessment": assessment,
            "recommendation": recommendation,
            "form": form,
        },
    )


# ---------------------------------------------------------------------------
# Phase 7: Quotation
# ---------------------------------------------------------------------------
# Standard balance-of-system line items every install needs regardless of
# what the sizing calculator matched — same shape as a real EPC proposal's
# bill of materials. Equipment lines are pre-filled from the
# SystemRecommendation since those prices are already known from the
# catalog; BOS lines are now pre-filled too, estimated from the target
# system size rather than left at KES 0, so the rep starts from a real
# number and adjusts rather than pricing every BOS line from scratch.
DEFAULT_BOS_LINE_ITEMS = [
    "DC integration sundries and accessories",
    "AC integration sundries and accessories",
    "Protection and earthing services",
    "Solar module mounting structures",
    "Safety systems",
    "Design, installation, testing and commissioning",
    "Logistics and delivery",
]

# Baseline KES-per-kW rate for each BOS line, derived from the Energex
# Solutions sample proposal's commercial 20kW 3-phase job (each item's
# quoted price / 20kW). The residential 12kW job in the same proposal
# itemizes its BOS differently but comes out to a similar total per-kW
# (~22.4k/kW vs ~19.5k/kW here), so scaling this commercial baseline by
# target size is a reasonable starting estimate for either segment — not
# a substitute for the rep checking it against the actual site.
BOS_RATE_KES_PER_KW = {
    "DC integration sundries and accessories": 2250,
    "AC integration sundries and accessories": 1500,
    "Protection and earthing services": 2400,
    "Solar module mounting structures": 4900,
    "Safety systems": 900,
    "Design, installation, testing and commissioning": 5000,
    "Logistics and delivery": 2500,
}
BOS_ESTIMATE_ROUNDING_KES = 500  # round each estimated line to the nearest this many KES


def _estimate_bos_price(description, system_size_kw):
    """KES estimate for one BOS line at a given system size, rounded to a
    clean figure. Falls back to 0 if we don't have a target size yet
    (e.g. quotation created before a recommendation exists) or no rate is
    defined for that description."""
    rate = BOS_RATE_KES_PER_KW.get(description)
    if not rate or not system_size_kw:
        return 0
    raw = float(rate) * float(system_size_kw)
    return round(raw / BOS_ESTIMATE_ROUNDING_KES) * BOS_ESTIMATE_ROUNDING_KES


def _default_quotation_line_items(project):
    """Seed rows for a brand-new quotation: matched equipment (priced from
    the catalog) plus the standard BOS items (pre-filled with a size-based
    estimate — see BOS_RATE_KES_PER_KW — for the rep to check and adjust)."""
    assessment = getattr(project, "site_assessment", None)
    recommendation = getattr(assessment, "recommendation", None) if assessment else None

    items = []
    if recommendation:
        if recommendation.panel and recommendation.panel_quantity:
            items.append({
                "category": QuotationLineItem.Category.EQUIPMENT,
                "description": str(recommendation.panel),
                "quantity": recommendation.panel_quantity,
                "unit_price_kes": recommendation.panel.unit_price_kes or 0,
            })
        if recommendation.inverter:
            items.append({
                "category": QuotationLineItem.Category.EQUIPMENT,
                "description": str(recommendation.inverter),
                "quantity": 1,
                "unit_price_kes": recommendation.inverter.unit_price_kes or 0,
            })
        if recommendation.battery and recommendation.battery_quantity:
            items.append({
                "category": QuotationLineItem.Category.EQUIPMENT,
                "description": str(recommendation.battery),
                "quantity": recommendation.battery_quantity,
                "unit_price_kes": recommendation.battery.unit_price_kes or 0,
            })

    system_size_kw = recommendation.target_system_size_kw if recommendation else None
    for description in DEFAULT_BOS_LINE_ITEMS:
        items.append({
            "category": QuotationLineItem.Category.BOS,
            "description": description,
            "quantity": 1,
            "unit_price_kes": _estimate_bos_price(description, system_size_kw),
        })
    return items


@login_required
def quotation_edit(request, customer_id):
    """
    Create (on first visit) or edit the project's quotation. Creating
    pre-fills the bill of materials from the SystemRecommendation plus the
    standard BOS line items, matching the shape of a real EPC proposal —
    the rep then adjusts quantities/prices before sending it out.
    """
    customer, denied = _get_customer_for_user(request, customer_id)
    if denied:
        return denied

    project = customer.projects.order_by("-created_at").first()
    if project is None:
        messages.error(request, "This client has no project to quote.")
        return redirect("client_detail", customer_id=customer.customer_id)

    quotation = getattr(project, "quotation", None)
    creating = quotation is None

    if creating:
        assessment = getattr(project, "site_assessment", None)
        recommendation = getattr(assessment, "recommendation", None) if assessment else None
        if recommendation is None or not recommendation.is_reviewed:
            messages.error(
                request,
                "This project's recommendation needs an engineer's sign-off before it can be quoted.",
            )
            return redirect("site_assessment_recommendation", customer_id=customer.customer_id)
        quotation = Quotation(project=project)

    if request.method == "POST":
        form = QuotationForm(request.POST, instance=quotation)
        if form.is_valid():
            with transaction.atomic():
                quotation = form.save(commit=False)
                quotation.project = project
                if creating:
                    quotation.prepared_by = request.user
                quotation.save()

            LineItemFormSet = quotation_line_item_formset(extra=0)
            formset = LineItemFormSet(request.POST, instance=quotation)
            if formset.is_valid():
                formset.save()
                messages.success(request, f"Quotation {quotation.quotation_id} saved.")
                return redirect("quotation_detail", customer_id=customer.customer_id)
        else:
            LineItemFormSet = quotation_line_item_formset(extra=0)
            formset = LineItemFormSet(request.POST, instance=quotation)
    else:
        form = QuotationForm(instance=quotation)
        if creating:
            initial = _default_quotation_line_items(project)
            LineItemFormSet = quotation_line_item_formset(extra=len(initial))
            formset = LineItemFormSet(queryset=QuotationLineItem.objects.none(), initial=initial)
        else:
            LineItemFormSet = quotation_line_item_formset(extra=1)
            formset = LineItemFormSet(instance=quotation)

    return render(
        request,
        "project/quotation_form.html",
        {
            "form": form,
            "formset": formset,
            "customer": customer,
            "project": project,
            "quotation": quotation,
            "creating": creating,
        },
    )


@login_required
def quotation_detail(request, customer_id):
    customer, denied = _get_customer_for_user(request, customer_id)
    if denied:
        return denied

    project = customer.projects.order_by("-created_at").first()
    quotation = get_object_or_404(Quotation, project=project)

    return render(
        request,
        "project/quotation_detail.html",
        {
            "customer": customer,
            "project": project,
            "quotation": quotation,
            "line_items": quotation.line_items.all(),
        },
    )


@login_required
def quotation_download(request, customer_id):
    """
    Same document as quotation_print, rendered server-side to an actual
    PDF file and returned as a download — quotation_print relies on the
    browser's own Ctrl+P / "Save as PDF", which not everyone has as an
    obvious option (mobile browsers especially), so this gives a direct
    "the file is now on your device" download instead.

    Requires xhtml2pdf (`pip install xhtml2pdf`) — pure-Python (reportlab
    under the hood), no system libraries to install, which is what makes
    it a better fit than WeasyPrint on machines where GTK/Pango/Cairo
    aren't set up (e.g. Windows without the GTK runtime).
    Imported inside the view rather than at module level so the rest of
    the app still runs if it's not installed yet; the view just falls
    back to the print page with a message instead of crashing.

    Renders "project/quotation_pdf.html" rather than quotation_print.html:
    xhtml2pdf's CSS support is much narrower than a browser's (no CSS
    variables, no flexbox/grid, no external Google Fonts, limited
    @media), so the PDF has its own self-contained, xhtml2pdf-safe
    template with the same design tokens hardcoded as literal values.
    Keep the two templates in sync by hand if the quotation layout changes.
    """
    customer, denied = _get_customer_for_user(request, customer_id)
    if denied:
        return denied

    project = customer.projects.order_by("-created_at").first()
    quotation = get_object_or_404(Quotation, project=project)

    try:
        from xhtml2pdf import pisa
    except ImportError:
        messages.error(
            request,
            "PDF download isn't set up on this server yet — xhtml2pdf "
            "isn't installed. Use Print / Save as PDF for now.",
        )
        return redirect("quotation_print", customer_id=customer.customer_id)

    html_string = render_to_string(
        "project/quotation_pdf.html",
        {
            "customer": customer,
            "project": project,
            "quotation": quotation,
            "line_items": quotation.line_items.all(),
        },
        request=request,
    )

    buffer = io.BytesIO()
    result = pisa.CreatePDF(html_string, dest=buffer)
    if result.err:
        messages.error(
            request,
            "PDF download couldn't be generated — there was an error "
            "rendering the quotation to PDF. Use Print / Save as PDF for "
            "now.",
        )
        return redirect("quotation_print", customer_id=customer.customer_id)

    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{quotation.quotation_id}.pdf"'
    return response


@login_required
def quotation_print(request, customer_id):
    """Standalone printable view (no sidebar) — Ctrl+P / 'Save as PDF' from here."""
    customer, denied = _get_customer_for_user(request, customer_id)
    if denied:
        return denied

    project = customer.projects.order_by("-created_at").first()
    quotation = get_object_or_404(Quotation, project=project)

    return render(
        request,
        "project/quotation_print.html",
        {
            "customer": customer,
            "project": project,
            "quotation": quotation,
            "line_items": quotation.line_items.all(),
        },
    )


@login_required
@require_POST
def quotation_send(request, customer_id):
    customer, denied = _get_customer_for_user(request, customer_id)
    if denied:
        return denied

    project = customer.projects.order_by("-created_at").first()
    quotation = get_object_or_404(Quotation, project=project)
    quotation.status = Quotation.Status.SENT
    quotation.sent_at = timezone.now()
    quotation.save(update_fields=["status", "sent_at"])

    messages.success(request, f"{quotation.quotation_id} marked as sent to the client.")
    return redirect("quotation_detail", customer_id=customer.customer_id)


@login_required
@require_POST
def quotation_approve(request, customer_id):
    """Records the client's approval (per the workflow's signed confirmation
    step) and advances the project into Approved."""
    customer, denied = _get_customer_for_user(request, customer_id)
    if denied:
        return denied

    project = customer.projects.order_by("-created_at").first()
    quotation = get_object_or_404(Quotation, project=project)

    with transaction.atomic():
        quotation.status = Quotation.Status.APPROVED
        quotation.approved_at = timezone.now()
        quotation.save(update_fields=["status", "approved_at"])
        project.status = Project.Status.APPROVED
        project.save(update_fields=["status"])

    messages.success(
        request, f"{quotation.quotation_id} approved — {project.project_id} moved to Approved."
    )
    return redirect("quotation_detail", customer_id=customer.customer_id)


@login_required
@require_POST
def quotation_reject(request, customer_id):
    customer, denied = _get_customer_for_user(request, customer_id)
    if denied:
        return denied

    project = customer.projects.order_by("-created_at").first()
    quotation = get_object_or_404(Quotation, project=project)
    quotation.status = Quotation.Status.REJECTED
    quotation.rejected_at = timezone.now()
    quotation.save(update_fields=["status", "rejected_at"])

    messages.info(request, f"{quotation.quotation_id} marked as rejected — revise and re-send when ready.")
    return redirect("quotation_detail", customer_id=customer.customer_id)


@login_required
def quotation_list(request):
    """
    Every quotation the user can see, as a filterable table. Each row opens
    that client's quotation (quotation_detail). Site engineers only see
    quotations for their own clients, same rule as everywhere else.
    """
    all_rows = list(quotations_for_user(request.user))
    for q in all_rows:
        # quotation_detail always shows the client's latest project, so a
        # quotation on an older project can't be opened from here.
        q.is_openable = q.project_id == q.latest_project_id

    status = request.GET.get("status", "").strip()
    if status not in Quotation.Status.values:
        status = ""
    query = request.GET.get("q", "").strip().lower()

    counts = {value: 0 for value in Quotation.Status.values}
    for q in all_rows:
        counts[q.status] += 1

    def _sum(rows, *statuses):
        return sum((r.total_kes for r in rows if r.status in statuses), 0)

    rows = all_rows
    if status:
        rows = [q for q in rows if q.status == status]
    if query:
        rows = [
            q for q in rows
            if query in q.project.customer.full_name.lower()
            or query in q.project.customer.customer_id.lower()
            or query in q.quotation_id.lower()
        ]

    status_tabs = [{"value": "", "label": "All", "count": len(all_rows)}] + [
        {"value": value, "label": label, "count": counts[value]}
        for value, label in Quotation.Status.choices
    ]

    return render(
        request,
        "project/quotation_list.html",
        {
            "quotations": rows,
            "status_tabs": status_tabs,
            "active_status": status,
            "query": request.GET.get("q", "").strip(),
            "draft_count": counts[Quotation.Status.DRAFT],
            "awaiting_count": counts[Quotation.Status.SENT],
            "awaiting_value": _sum(all_rows, Quotation.Status.SENT),
            "approved_value": _sum(all_rows, Quotation.Status.APPROVED),
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


def get_catalog_registry():
    """
    The built-in categories (panels, inverters, batteries) plus every
    user-created ProductCategory, keyed by the slug used in the URLs.
    """
    registry = dict(CATALOG_REGISTRY)
    for category in ProductCategory.objects.all():
        registry[category.slug] = {
            "model": Product,
            "form": ProductForm,
            "label": category.name,
            "singular": "item",
            "category": category,
        }
    return registry


def _entry_items(entry):
    """All rows belonging to one catalog entry (a category's products, or a whole equipment table)."""
    category = entry.get("category")
    if category is not None:
        return entry["model"].objects.filter(category=category)
    return entry["model"].objects.all()

def _catalog_overview():
    """
    Used by the dashboard: total active catalog items. Stock levels are no
    longer tracked, so the second value (low-stock items) is always empty;
    it's kept so the dashboard and sidebar callers don't need to change.
    """
    total_active = sum(
        _entry_items(entry).filter(is_active=True).count()
        for entry in get_catalog_registry().values()
    )
    return total_active, []


def _catalog_entry(kind):
    entry = get_catalog_registry().get(kind)
    if entry is None:
        raise Http404("Unknown catalog category.")
    return entry


def _catalog_home_context(category_form, open_category_modal=False):
    catalog_cards = [
        {"kind": kind, "entry": entry, "count": _entry_items(entry).count()}
        for kind, entry in get_catalog_registry().items()
    ]
    return {
        "catalog_cards": catalog_cards,
        "category_form": category_form,
        "open_category_modal": open_category_modal,
    }


@login_required
def category_create(request):
    """
    Handles the "Add category" modal on the catalog page. There's no page of
    its own: a successful save goes back to the catalog, and a failed one
    re-renders the catalog with the modal reopened to show the errors.
    """
    if request.method != "POST":
        return redirect("catalog_home")

    form = ProductCategoryForm(request.POST)
    if form.is_valid():
        category = form.save()
        messages.success(request, f"Category \u201c{category.name}\u201d added. You can now add items to it.")
        return redirect("catalog_home")

    return render(
        request,
        "project/catalog_home.html",
        _catalog_home_context(form, open_category_modal=True),
    )


@login_required
def catalog_home(request):
    """Landing page for equipment catalog management."""
    return render(
        request,
        "project/catalog_home.html",
        _catalog_home_context(ProductCategoryForm()),
    )


@login_required
def equipment_list(request, kind):
    entry = _catalog_entry(kind)
    items = _entry_items(entry)

    query = request.GET.get("q", "").strip()
    if query:
        condition = Q(name__icontains=query)
        if any(f.name == "brand" for f in entry["model"]._meta.get_fields()):
            condition |= Q(brand__icontains=query)
        items = items.filter(condition)

    return render(
        request,
        "project/equipment_list.html",
        {"kind": kind, "entry": entry, "items": items, "query": query},
    )


@login_required
def equipment_form(request, kind, pk=None):
    entry = _catalog_entry(kind)
    instance = get_object_or_404(_entry_items(entry), pk=pk) if pk else None
    form = entry["form"](request.POST or None, instance=instance)

    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if entry.get("category") is not None:
            obj.category = entry["category"]
        obj.save()
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
    entry = _catalog_entry(kind)
    item = get_object_or_404(_entry_items(entry), pk=pk)
    item.delete()
    messages.success(request, f"{entry['singular'].capitalize()} removed from the catalog.")
    return redirect("equipment_list", kind=kind)