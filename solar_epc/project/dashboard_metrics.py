"""
Everything the dashboard shows, computed in one place and returned as plain
JSON-serialisable data.

The same payload feeds two callers, so the page and its live refresh can
never disagree:
  - views.dashboard       embeds it in the page for an instant first paint
  - views.dashboard_data  returns it as JSON for the 30-second refresh

Scoping matches the rest of the app: site engineers only see their own
clients/projects/quotations, admins see everything.

Tunable thresholds are named constants below rather than buried in queries.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import (
    Count, DecimalField, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum,
)
from django.db.models.functions import Coalesce, TruncMonth
from django.urls import reverse
from django.utils import timezone

from .models import (
    Customer, Project, Quotation, SiteAssessment, SystemRecommendation, User,
)

MONTHS_SHOWN = 6            # trend charts: this month + the 5 before it
STALE_LEAD_DAYS = 7         # a lead with no movement for this long needs a nudge
EXPIRY_WARNING_DAYS = 7     # flag sent quotations expiring within this window
LIST_LIMIT = 6              # rows in the short tables
ACTIVITY_LIMIT = 10

S = Project.Status
INACTIVE_STATUSES = [S.COMPLETED, S.CANCELLED]
# Stages a project passes through, in order. Cancelled is reported separately.
STAGE_ORDER = [
    S.LEAD, S.SITE_ASSESSMENT, S.ENGINEER_REVIEW, S.QUOTATION, S.APPROVED,
    S.INVOICED, S.IN_PROGRESS, S.COMMISSIONED, S.COMPLETED,
]
# Projects whose planned capacity counts as "in the pipeline".
CAPACITY_STATUSES = [
    S.ENGINEER_REVIEW, S.QUOTATION, S.APPROVED, S.INVOICED, S.IN_PROGRESS,
]

_MONEY = DecimalField(max_digits=18, decimal_places=2)
_LINE_TOTAL = ExpressionWrapper(
    F("line_items__quantity") * F("line_items__unit_price_kes"), output_field=_MONEY
)


# ---------------------------------------------------------------- helpers
def _num(value) -> float:
    return float(round(Decimal(value or 0), 2))


def _iso(dt):
    return dt.isoformat() if dt else None


def _month_start(dt):
    local = timezone.localtime(dt)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _add_months(month_start, n):
    index = month_start.year * 12 + (month_start.month - 1) + n
    return month_start.replace(year=index // 12, month=index % 12 + 1)


def _key(dt):
    local = timezone.localtime(dt)
    return (local.year, local.month)


def _delta_pct(current, previous):
    """Percent change vs the previous period; None when there's no baseline."""
    if not previous:
        return None
    return round((current - previous) / previous * 100)


def _scopes(user):
    customers = Customer.objects.all()
    projects = Project.objects.all()
    assessments = SiteAssessment.objects.all()
    quotes = Quotation.objects.all()
    if user.is_site_engineer:
        customers = customers.filter(assigned_rep=user)
        projects = projects.filter(assigned_rep=user)
        assessments = assessments.filter(
            Q(project__assigned_rep=user) | Q(assigned_engineer=user)
        )
        quotes = quotes.filter(project__customer__assigned_rep=user)
    return customers, projects, assessments, quotes


def _quote_rows(quotes):
    """One query: every in-scope quotation with its total and openability."""
    latest_project = (
        Project.objects.filter(customer=OuterRef("project__customer"))
        .order_by("-created_at")
        .values("id")[:1]
    )
    return list(
        quotes.annotate(latest_project_id=Subquery(latest_project))
        .values(
            "id", "quotation_id", "status", "created_at", "sent_at",
            "approved_at", "rejected_at", "validity_days", "project_id",
            "latest_project_id",
            "project__customer__customer_id",
            "project__customer__full_name",
            "project__customer__assigned_rep_id",
        )
        .annotate(total=Coalesce(Sum(_LINE_TOTAL), Decimal("0"), output_field=_MONEY))
    )


def _client_url(customer_id):
    return reverse("client_detail", args=[customer_id])


# ------------------------------------------------------------------ main
def build_dashboard_data(user, catalog_total_active=None):
    now = timezone.now()
    customers, projects, assessments, quotes = _scopes(user)
    rows = _quote_rows(quotes)

    this_month = _month_start(now)
    last_month = _add_months(this_month, -1)
    first_shown = _add_months(this_month, -(MONTHS_SHOWN - 1))
    month_keys = [
        (m.year, m.month)
        for m in (_add_months(first_shown, i) for i in range(MONTHS_SHOWN))
    ]
    month_labels = [
        _add_months(first_shown, i).strftime("%b") for i in range(MONTHS_SHOWN)
    ]

    # ----- monthly series: new projects (leads) and won quotations
    leads_by_month = {k: 0 for k in month_keys}
    for row in (
        projects.filter(created_at__gte=first_shown)
        .annotate(m=TruncMonth("created_at"))
        .values("m").annotate(n=Count("id"))
    ):
        k = (row["m"].year, row["m"].month)
        if k in leads_by_month:
            leads_by_month[k] = row["n"]

    won_count = {k: 0 for k in month_keys}
    won_value = {k: Decimal("0") for k in month_keys}
    for r in rows:
        if r["status"] == Quotation.Status.APPROVED and r["approved_at"]:
            k = _key(r["approved_at"])
            if k in won_count:
                won_count[k] += 1
                won_value[k] += r["total"]

    cur_key, prev_key = month_keys[-1], month_keys[-2]
    leads_now, leads_prev = leads_by_month[cur_key], leads_by_month[prev_key]
    won_now, won_prev = won_value[cur_key], won_value[prev_key]

    # ----- headline numbers
    total_clients = customers.count()
    clients_new = customers.filter(created_at__gte=this_month).count()
    active_projects = projects.exclude(status__in=INACTIVE_STATUSES).count()

    sent = [r for r in rows if r["status"] == Quotation.Status.SENT]
    approved = [r for r in rows if r["status"] == Quotation.Status.APPROVED]
    rejected = [r for r in rows if r["status"] == Quotation.Status.REJECTED]
    drafts = [r for r in rows if r["status"] == Quotation.Status.DRAFT]
    decided = len(approved) + len(rejected)

    capacity_kw = SystemRecommendation.objects.filter(
        site_assessment__project__in=projects.filter(status__in=CAPACITY_STATUSES)
    ).aggregate(kw=Sum("target_system_size_kw"))["kw"]

    # ----- what needs attention
    today_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    overdue_visits = assessments.filter(
        status=SiteAssessment.Status.SCHEDULED, scheduled_date__lt=today_start
    ).count()
    review_pending = projects.filter(status=S.ENGINEER_REVIEW).count()
    stale_leads = projects.filter(
        status=S.LEAD, created_at__lt=now - timedelta(days=STALE_LEAD_DAYS)
    ).count()

    def _expires(r):
        return r["sent_at"] + timedelta(days=r["validity_days"]) if r["sent_at"] else None

    expiring = [
        r for r in sent
        if _expires(r) and _expires(r) <= now + timedelta(days=EXPIRY_WARNING_DAYS)
    ]

    quotation_list_url = reverse("quotation_list")
    client_list_url = reverse("client_list")
    attention = [
        {"key": "overdue_visits", "tone": "danger", "count": overdue_visits,
         "text": "site visits are overdue", "url": client_list_url},
        {"key": "expiring", "tone": "danger", "count": len(expiring),
         "text": "sent quotations expired or expiring within "
                 f"{EXPIRY_WARNING_DAYS} days",
         "url": f"{quotation_list_url}?status={Quotation.Status.SENT}"},
        {"key": "review", "tone": "warn", "count": review_pending,
         "text": "recommendations await engineer review", "url": client_list_url},
        {"key": "drafts", "tone": "warn", "count": len(drafts),
         "text": "draft quotations haven't been sent",
         "url": f"{quotation_list_url}?status={Quotation.Status.DRAFT}"},
        {"key": "stale", "tone": "info", "count": stale_leads,
         "text": f"leads have had no movement for {STALE_LEAD_DAYS}+ days",
         "url": client_list_url},
    ]
    attention = [a for a in attention if a["count"] > 0]

    kpis = {
        "clients": {"value": total_clients, "new_this_month": clients_new},
        "leads": {"value": leads_now, "previous": leads_prev,
                  "delta_pct": _delta_pct(leads_now, leads_prev)},
        "active_projects": {"value": active_projects,
                            "needs_action": sum(a["count"] for a in attention
                                                if a["key"] in ("overdue_visits", "review", "drafts"))},
        "awaiting": {"count": len(sent), "value": _num(sum((r["total"] for r in sent), Decimal("0")))},
        "won_month": {"value": _num(won_now), "previous": _num(won_prev),
                      "count": won_count[cur_key],
                      "delta_pct": _delta_pct(won_now, won_prev)},
        "win_rate": {"value": round(len(approved) / decided * 100) if decided else None,
                     "won": len(approved), "lost": len(rejected)},
        "capacity_kw": _num(capacity_kw),
        "catalog_items": catalog_total_active if user.is_admin_role else None,
    }

    # ----- charts
    stage_counts = {
        row["status"]: row["n"]
        for row in projects.values("status").annotate(n=Count("id"))
    }
    pipeline = [
        {"key": s.value, "label": s.label, "count": stage_counts.get(s.value, 0)}
        for s in STAGE_ORDER
    ]
    pipeline.append({
        "key": S.CANCELLED.value, "label": S.CANCELLED.label,
        "count": stage_counts.get(S.CANCELLED.value, 0),
    })

    mix_counts = {
        row["customer_type"]: row["n"]
        for row in customers.values("customer_type").annotate(n=Count("id"))
    }
    customer_mix = [
        {"key": t.value, "label": t.label, "count": mix_counts.get(t.value, 0)}
        for t in Customer.CustomerType
    ]

    charts = {
        "pipeline": pipeline,
        "monthly": {
            "labels": month_labels,
            "leads": [leads_by_month[k] for k in month_keys],
            "won_count": [won_count[k] for k in month_keys],
            "won_value": [_num(won_value[k]) for k in month_keys],
        },
        "customer_mix": customer_mix,
    }

    # ----- tables
    upcoming = (
        assessments.filter(status=SiteAssessment.Status.SCHEDULED, scheduled_date__gte=today_start)
        .select_related("project__customer", "assigned_engineer")
        .order_by("scheduled_date")[:LIST_LIMIT]
    )
    visits = [{
        "customer": a.project.customer.full_name,
        "customer_id": a.project.customer.customer_id,
        "when": _iso(a.scheduled_date),
        "engineer": (a.assigned_engineer.get_full_name() or a.assigned_engineer.username)
        if a.assigned_engineer else None,
        "url": reverse("site_assessment_survey", args=[a.project.customer.customer_id]),
    } for a in upcoming]

    awaiting = []
    for r in sorted(sent, key=lambda r: r["sent_at"] or now)[:LIST_LIMIT]:
        expires = _expires(r)
        openable = r["project_id"] == r["latest_project_id"]
        awaiting.append({
            "quotation_id": r["quotation_id"],
            "customer": r["project__customer__full_name"],
            "total": _num(r["total"]),
            "sent_at": _iso(r["sent_at"]),
            "expires_at": _iso(expires),
            "days_left": (expires - now).days if expires else None,
            "urgent": bool(expires and expires <= now + timedelta(days=EXPIRY_WARNING_DAYS)),
            "url": reverse("quotation_detail", args=[r["project__customer__customer_id"]])
            if openable else None,
        })

    recent_clients = [{
        "name": c.full_name, "customer_id": c.customer_id,
        "type": c.get_customer_type_display(), "county": c.county,
        "created_at": _iso(c.created_at), "url": _client_url(c.customer_id),
    } for c in customers.order_by("-created_at")[:5]]

    # ----- activity feed (built from the timestamps records already carry)
    events = []
    for c in customers.order_by("-created_at")[:ACTIVITY_LIMIT]:
        events.append({"ts": c.created_at, "tone": "info", "url": _client_url(c.customer_id),
                       "text": f"{c.full_name} registered as a client"})
    for a in (assessments.filter(completed_at__isnull=False)
              .select_related("project__customer").order_by("-completed_at")[:ACTIVITY_LIMIT]):
        cust = a.project.customer
        events.append({"ts": a.completed_at, "tone": "info", "url": _client_url(cust.customer_id),
                       "text": f"Site survey completed for {cust.full_name}"})
    for r in rows:
        name, url = r["project__customer__full_name"], _client_url(r["project__customer__customer_id"])
        if r["sent_at"]:
            events.append({"ts": r["sent_at"], "tone": "info", "url": url,
                           "text": f"Quotation {r['quotation_id']} sent to {name}"})
        if r["approved_at"]:
            events.append({"ts": r["approved_at"], "tone": "success", "url": url,
                           "text": f"{name} approved {r['quotation_id']}"})
        if r["rejected_at"]:
            events.append({"ts": r["rejected_at"], "tone": "danger", "url": url,
                           "text": f"{name} rejected {r['quotation_id']}"})
    events.sort(key=lambda e: e["ts"], reverse=True)
    activity = [{**e, "ts": _iso(e["ts"])} for e in events[:ACTIVITY_LIMIT]]

    # ----- team workload (admins only)
    team = None
    if user.is_admin_role:
        won_by_rep, won_val_by_rep = {}, {}
        for r in approved:
            rep = r["project__customer__assigned_rep_id"]
            won_by_rep[rep] = won_by_rep.get(rep, 0) + 1
            won_val_by_rep[rep] = won_val_by_rep.get(rep, Decimal("0")) + r["total"]
        reps = (
            User.objects.filter(role=User.Role.SITE_ENGINEER, is_active=True)
            .annotate(
                clients=Count("assigned_customers", distinct=True),
                active=Count("assigned_projects", distinct=True,
                             filter=~Q(assigned_projects__status__in=INACTIVE_STATUSES)),
            )
            .order_by("-active", "-clients", "username")
        )
        team = [{
            "name": u.get_full_name() or u.username, "clients": u.clients, "active": u.active,
            "won": won_by_rep.get(u.id, 0), "won_value": _num(won_val_by_rep.get(u.id)),
        } for u in reps]

    return {
        "generated_at": _iso(now),
        "is_admin": bool(user.is_admin_role),
        "kpis": kpis,
        "attention": attention,
        "charts": charts,
        "visits": visits,
        "awaiting": awaiting,
        "recent_clients": recent_clients,
        "activity": activity,
        "team": team,
    }