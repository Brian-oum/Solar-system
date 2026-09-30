"""
Context processor that feeds the sidebar in base.html.

Register it in settings.py (adjust "project" to your app's label):

    TEMPLATES[0]["OPTIONS"]["context_processors"] += [
        "project.sidebar_context.sidebar",
    ]

Everything here is read-only and follows the same access rules as views.py:
site engineers only see their own assigned customers/projects, admins see all.
"""
from django.db.models import Count
from django.urls import reverse

from .models import Customer, Project, Quotation, SiteAssessment

# Short sidebar labels for each project status, in lifecycle order.
# Cancelled is left out on purpose: it isn't a stage of the pipeline.
PIPELINE_LABELS = [
    (Project.Status.LEAD, "Leads"),
    (Project.Status.SITE_ASSESSMENT, "Site assessment"),
    (Project.Status.ENGINEER_REVIEW, "Engineer review"),
    (Project.Status.QUOTATION, "Quotation"),
    (Project.Status.APPROVED, "Approved"),
    (Project.Status.INVOICED, "Invoiced"),
    (Project.Status.IN_PROGRESS, "Installing"),
    (Project.Status.COMMISSIONED, "Commissioned"),
    (Project.Status.COMPLETED, "Completed"),
]


def _pipeline(projects, active_status):
    raw = {row["status"]: row["n"] for row in projects.values("status").annotate(n=Count("id"))}
    biggest = max([raw.get(s, 0) for s, _ in PIPELINE_LABELS] + [1])
    return [
        {
            "key": status.value,
            "label": label,
            "count": raw.get(status, 0),
            "pct": round(raw.get(status, 0) / biggest * 100),
            "active": active_status == status.value,
        }
        for status, label in PIPELINE_LABELS
    ]


def _current_client(request, user):
    """
    When the URL carries a customer_id, build the step tracker for that
    customer's latest project. Mirrors the sequencing in views._project_actions.
    """
    customer_id = request.resolver_match.kwargs.get("customer_id")
    if not customer_id:
        return None
    customer = Customer.objects.filter(customer_id=customer_id).first()
    if customer is None:
        return None
    if user.is_site_engineer and customer.assigned_rep_id != user.id:
        return None

    project = (
        customer.projects
        .select_related("site_assessment", "site_assessment__recommendation", "quotation")
        .order_by("-created_at")
        .first()
    )
    if project is None:
        return {"customer": customer, "project": None, "steps": []}

    assessment = getattr(project, "site_assessment", None)
    recommendation = getattr(assessment, "recommendation", None) if assessment else None
    quotation = getattr(project, "quotation", None)

    booked = assessment is not None
    surveyed = booked and assessment.status == SiteAssessment.Status.COMPLETED
    reviewed = bool(recommendation and recommendation.is_reviewed)
    cid = customer.customer_id
    url_name = request.resolver_match.url_name or ""

    def step(label, hint, done, locked, name, match):
        return {
            "label": label,
            "hint": hint,
            "state": "done" if done else ("locked" if locked else "current"),
            "url": None if locked else reverse(name, args=[cid]),
            "active": match(url_name),
        }

    steps = [
        step("Site visit",
             assessment.get_status_display() if booked else "Not booked",
             booked, False, "site_assessment_schedule",
             lambda n: n == "site_assessment_schedule"),
        step("Site survey",
             "Completed" if surveyed else ("In progress" if booked else "Book a visit first"),
             surveyed, not booked, "site_assessment_survey",
             lambda n: n == "site_assessment_survey"),
        step("Recommendation",
             "Reviewed" if reviewed else ("Needs review" if surveyed else "Finish the survey first"),
             reviewed, not surveyed, "site_assessment_recommendation",
             lambda n: n == "site_assessment_recommendation"),
        step("Quotation",
             quotation.get_status_display() if quotation else ("Ready to create" if reviewed else "Review recommendation first"),
             bool(quotation and quotation.status == Quotation.Status.APPROVED),
             not reviewed and quotation is None,
             "quotation_detail" if quotation else "quotation_edit",
             lambda n: n.startswith("quotation")),
    ]

    return {
        "customer": customer,
        "project": project,
        "status": project.status,
        "status_label": project.get_status_display(),
        "steps": steps,
        "has_quotation": quotation is not None,
        "profile_active": url_name == "client_detail",
        "profile_url": reverse("client_detail", args=[cid]),
        "print_url": reverse("quotation_print", args=[cid]) if quotation else None,
        "download_url": reverse("quotation_download", args=[cid]) if quotation else None,
    }


def _catalog(user):
    # Imported here because views.py imports models at module load.
    from .views import CATALOG_REGISTRY, _catalog_overview

    _total, low_rows = _catalog_overview()
    low_by_kind = {}
    for row in low_rows:
        low_by_kind[row["kind"]] = low_by_kind.get(row["kind"], 0) + 1
    return {
        "low_total": len(low_rows),
        "kinds": [
            {
                "kind": kind,
                "label": entry["label"],
                "count": entry["model"].objects.filter(is_active=True).count(),
                "low": low_by_kind.get(kind, 0),
            }
            for kind, entry in CATALOG_REGISTRY.items()
        ],
    }


def sidebar(request):
    user = getattr(request, "user", None)
    match = getattr(request, "resolver_match", None)
    if user is None or not user.is_authenticated or match is None:
        return {}

    customers = Customer.objects.all()
    projects = Project.objects.all()
    if user.is_site_engineer:
        customers = customers.filter(assigned_rep=user)
        projects = projects.filter(assigned_rep=user)

    data = {
        "client_count": customers.count(),
        "scope_label": "My clients" if user.is_site_engineer else "Clients",
        "pipeline": _pipeline(projects, request.GET.get("status", "")),
        "active_total": projects.exclude(
            status__in=[Project.Status.COMPLETED, Project.Status.CANCELLED]
        ).count(),
        "client": _current_client(request, user),
    }
    if user.is_admin_role:
        data["catalog"] = _catalog(user)
    return {"sidebar": data}