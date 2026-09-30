"""
Rule-based solar sizing calculator.

Takes a completed (or in-progress) SiteAssessment and produces a
SystemRecommendation: a target system size, target battery capacity, and
the best-fit active catalog items for each.

This is deliberately a plain set of rules, not a model or optimizer — every
constant below is named and adjustable so the sizing logic can be reviewed
and tuned without touching the calling code. Swap it for something smarter
later without changing views.py or the templates that call
`calculate_recommendation()`.

Assumptions (Kenya-typical, adjust to taste):
- PEAK_SUN_HOURS: average full-sun-equivalent hours/day at the array.
- PERFORMANCE_RATIO: system losses (wiring, temperature, inverter, soiling).
- SHADING_DERATE: multiplies peak sun hours down for partial shading.
- Panel footprint: ~1.8 m² per 450W panel if a specific panel's own
  panel_area_sqm isn't set on the catalog item.

Devices captured on the survey (SiteLoadItem) feed three things:
- Consumption: used when there's no monthly bill figure; when both exist the
  bills win for sizing and a large disagreement is flagged.
- Inverter: must cover the diversified running load and, given a typical
  short-term overload rating, the worst single start-up surge. Any
  three-phase device pushes the pick to a three-phase inverter.
- Battery: sized on the devices marked "keep on during outage" (all devices
  for off-grid sites), not on the whole-house average.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import Battery, Customer, Inverter, SiteAssessment, SolarPanel, SystemRecommendation

PEAK_SUN_HOURS = 4.5
PERFORMANCE_RATIO = 0.80

SHADING_DERATE = {
    SiteAssessment.ShadingLevel.NONE: 1.00,
    SiteAssessment.ShadingLevel.MINIMAL: 0.90,
    SiteAssessment.ShadingLevel.MODERATE: 0.75,
    SiteAssessment.ShadingLevel.HEAVY: 0.55,
    "": 1.00,
}

DEFAULT_PANEL_AREA_SQM = 2.0  # fallback footprint per panel if catalog item has none
MIN_SYSTEM_SIZE_KW = 1.0

# Backup autonomy target, in hours, by supply situation.
AUTONOMY_HOURS = {
    SiteAssessment.SupplyType.OFF_GRID: 12,
    SiteAssessment.SupplyType.GENERATOR: 8,
    SiteAssessment.SupplyType.GRID_SINGLE: 4,
    SiteAssessment.SupplyType.GRID_THREE: 4,
    "": 4,
}

# ---------------------------------------------------------------------------
# Segment-aware tuning
# ---------------------------------------------------------------------------
# Customer.customer_type isn't itself a sizing input (consumption + roof
# survey still drive the numbers) but it does change what a "sensible"
# system looks like: a 1.2kW recommendation for a factory is almost
# certainly a data problem, not a real answer, and a commercial/industrial
# site is far more likely to actually be wired three-phase and worth
# quoting that way even when the assessor left current_supply_type blank.
# Keeping these as named, per-segment constants (rather than branching
# logic scattered through the picker functions) keeps the same
# "adjustable without touching calling code" property the rest of this
# module aims for.

# Sensible floor for the sized system, by customer segment. Prevents a
# tiny/incomplete consumption reading from producing an unrealistically
# small commercial or industrial recommendation.
SEGMENT_MIN_SYSTEM_SIZE_KW = {
    Customer.CustomerType.RESIDENTIAL: 1.0,
    Customer.CustomerType.COMMERCIAL: 5.0,
    Customer.CustomerType.INDUSTRIAL: 10.0,
}

# Segments where we bias toward a three-phase inverter even if the survey
# didn't explicitly record grid_three — commercial/industrial sites are
# disproportionately likely to be three-phase or worth upgrading to it,
# residential sites should stick to what the survey actually found.
SEGMENT_PREFERS_THREE_PHASE = {
    Customer.CustomerType.RESIDENTIAL: False,
    Customer.CustomerType.COMMERCIAL: True,
    Customer.CustomerType.INDUSTRIAL: True,
}

# Segments where, once a panel choice is within COMMERCIAL_PANEL_COST_TOLERANCE
# of the cheapest fit, we prefer fewer/higher-wattage panels over the
# absolute lowest KES/W — larger jobs are more sensitive to racking,
# labour and roof-run complexity than shaving a few shillings per watt.
SEGMENT_PREFERS_FEWER_PANELS = {
    Customer.CustomerType.RESIDENTIAL: False,
    Customer.CustomerType.COMMERCIAL: True,
    Customer.CustomerType.INDUSTRIAL: True,
}
COMMERCIAL_PANEL_COST_TOLERANCE = 0.03  # 3%

# ---------------------------------------------------------------------------
# Device-load tuning (see SiteLoadItem)
# ---------------------------------------------------------------------------
# Not every listed device runs at once. This is the share of the total
# connected running load assumed to be on together, by segment.
SEGMENT_DEMAND_FACTOR = {
    Customer.CustomerType.RESIDENTIAL: 0.60,
    Customer.CustomerType.COMMERCIAL: 0.70,
    Customer.CustomerType.INDUSTRIAL: 0.80,
}
DEFAULT_DEMAND_FACTOR = 0.70

# Hybrid inverters can typically deliver about 2x their rating for a few
# seconds, which is what motor/compressor start-up needs.
INVERTER_SURGE_RATIO = 2.0

# Flag when device-derived and bill-derived daily consumption differ by more
# than this fraction (of the bill figure).
LOAD_VS_BILL_TOLERANCE = 0.25


@dataclass
class SizingResult:
    daily_consumption_kwh: float | None = None
    target_system_size_kw: float | None = None
    target_battery_capacity_kwh: float | None = None
    backup_autonomy_hours: int = 0
    panel: SolarPanel | None = None
    panel_quantity: int = 0
    inverter: Inverter | None = None
    battery: Battery | None = None
    battery_quantity: int = 0
    # Device-load figures (None when no devices were captured on the survey).
    load_daily_kwh: float | None = None
    diversified_load_kw: float | None = None
    peak_surge_kw: float | None = None
    consumption_source: str = ""  # "bills" or "devices"
    warnings: list[str] = field(default_factory=list)


@dataclass
class LoadSummary:
    daily_kwh: float
    diversified_kw: float  # running load expected to be on together
    peak_surge_kw: float  # diversified load plus the largest single start-up
    backup_kwh: float  # energy the backup devices need within the autonomy window
    has_three_phase: bool
    has_essential: bool


def _summarise_loads(assessment: SiteAssessment, customer_type, autonomy_hours: int):
    """Roll the survey's device list up into the numbers the sizing rules
    need. Returns None if no devices were captured."""
    items = list(assessment.load_items.all())
    if not items:
        return None

    off_grid = assessment.current_supply_type == SiteAssessment.SupplyType.OFF_GRID
    # Off-grid there is no "outage" — everything has to keep running.
    backup_items = items if off_grid else [i for i in items if i.is_essential]

    running_w = sum(i.total_running_w for i in items)
    diversified_w = running_w * SEGMENT_DEMAND_FACTOR.get(customer_type, DEFAULT_DEMAND_FACTOR)
    largest_start_extra_w = max(
        i.rated_power_w * (float(i.surge_multiplier) - 1) for i in items
    )

    return LoadSummary(
        daily_kwh=sum(i.daily_kwh for i in items),
        diversified_kw=diversified_w / 1000,
        peak_surge_kw=(diversified_w + largest_start_extra_w) / 1000,
        backup_kwh=sum(
            i.total_running_w / 1000 * min(float(i.hours_per_day), autonomy_hours)
            for i in backup_items
        ),
        has_three_phase=any(i.is_three_phase for i in items),
        has_essential=bool(backup_items),
    )


def _available_area(assessment: SiteAssessment) -> float | None:
    """Prefer the explicit installable area; fall back to total roof area."""
    if assessment.available_installation_area_sqm is not None:
        return float(assessment.available_installation_area_sqm)
    if assessment.roof_area_sqm is not None:
        return float(assessment.roof_area_sqm)
    return None


def _pick_panel(
    target_kw: float,
    available_area_sqm: float | None,
    warnings: list[str],
    customer_type: str | None = None,
):
    """
    Among active panels, choose the one that hits the target kW
    within the available roof area at the lowest total cost. Falls back to
    whatever panel needs the fewest panels if nothing fits the area, and
    flags that with a warning rather than silently oversizing the roof.

    For commercial/industrial customers, a fit within
    COMMERCIAL_PANEL_COST_TOLERANCE of the cheapest option is retained as a
    candidate too, and among those we prefer whichever needs fewer panels —
    see SEGMENT_PREFERS_FEWER_PANELS above.
    """
    candidates = SolarPanel.objects.filter(is_active=True)
    if not candidates.exists():
        warnings.append("No active solar panels in the catalog.")
        return None, 0

    prefer_fewer_panels = SEGMENT_PREFERS_FEWER_PANELS.get(customer_type, False)

    best_fit = None
    best_fit_cost = None
    best_fit_count = None
    near_best_fits = []  # (panel, count, cost) within tolerance of best_fit_cost
    best_overall = None
    best_overall_count = None

    for panel in candidates:
        count = max(1, math.ceil((target_kw * 1000) / panel.wattage_w))
        if count * panel.wattage_w < target_kw * 1000 * 0.95:
            # This panel can't cover the target —
            # skip it as a fit candidate (still tracked as "best overall" below).
            fits_target = False
        else:
            fits_target = True

        area_needed = count * float(panel.panel_area_sqm or DEFAULT_PANEL_AREA_SQM)
        fits_area = available_area_sqm is None or area_needed <= available_area_sqm

        cost = float(panel.unit_price_kes or 0) * count

        if fits_target and fits_area:
            near_best_fits.append((panel, count, cost))
            if best_fit is None or cost < best_fit_cost:
                best_fit, best_fit_cost, best_fit_count = panel, cost, count

        # Track the smallest-panel-count fallback in case nothing fits area.
        if best_overall is None or count < best_overall_count:
            best_overall, best_overall_count = panel, count

    if best_fit is not None:
        if prefer_fewer_panels and best_fit_cost:
            tolerance = best_fit_cost * (1 + COMMERCIAL_PANEL_COST_TOLERANCE)
            within_tolerance = [c for c in near_best_fits if c[2] <= tolerance]
            if within_tolerance:
                panel, count, _cost = min(within_tolerance, key=lambda c: c[1])
                return panel, count
        return best_fit, best_fit_count

    if best_overall is not None:
        warnings.append(
            "No active panel combination fits the available roof area at the "
            "target size — showing the closest option; roof area may be a "
            "hard constraint here."
        )
        return best_overall, best_overall_count

    return None, 0


def _pick_inverter(
    target_kw: float,
    assessment: SiteAssessment,
    warnings: list[str],
    customer_type: str | None = None,
    needs_three_phase: bool = False,
):
    wants_three_phase = (
        needs_three_phase
        or assessment.current_supply_type == SiteAssessment.SupplyType.GRID_THREE
        or SEGMENT_PREFERS_THREE_PHASE.get(customer_type, False)
    )
    wants_hybrid = assessment.current_supply_type in (
        SiteAssessment.SupplyType.OFF_GRID,
        SiteAssessment.SupplyType.GENERATOR,
    ) or not assessment.has_existing_backup

    candidates = Inverter.objects.filter(is_active=True)
    if wants_three_phase:
        phase_matched = candidates.filter(phase=Inverter.Phase.THREE)
        candidates = phase_matched if phase_matched.exists() else candidates

    if wants_hybrid:
        hybrid_matched = candidates.filter(is_hybrid=True)
        candidates = hybrid_matched if hybrid_matched.exists() else candidates

    # Smallest inverter whose rated output still covers the target size.
    fitting = candidates.filter(capacity_kw__gte=target_kw).order_by("capacity_kw")
    inverter = fitting.first()
    if inverter:
        return inverter

    # Nothing covers the full target — fall back to the largest available
    # and flag it, rather than recommending nothing.
    largest = candidates.order_by("-capacity_kw").first()
    if largest:
        warnings.append(
            f"No active inverter rated ≥{target_kw:.1f}kW — "
            f"largest available is {largest.capacity_kw}kW."
        )
        return largest

    warnings.append("No active inverters in the catalog.")
    return None


def _pick_battery(target_kwh: float, warnings: list[str]):
    candidates = Battery.objects.filter(is_active=True)
    if not candidates.exists():
        warnings.append("No active batteries in the catalog.")
        return None, 0

    best = None
    best_count = None
    best_cost = None
    for battery in candidates:
        usable_kwh = float(battery.capacity_kwh) * (battery.usable_depth_of_discharge_percent / 100)
        if usable_kwh <= 0:
            continue
        count = max(1, math.ceil(target_kwh / usable_kwh))
        cost = float(battery.unit_price_kes or 0) * count
        if best is None or cost < best_cost:
            best, best_count, best_cost = battery, count, cost

    if best is None:
        warnings.append("No suitable battery configuration found in the catalog.")
        return None, 0
    return best, best_count


def compute_sizing(assessment: SiteAssessment) -> SizingResult:
    result = SizingResult()

    customer_type = assessment.project.customer.customer_type
    autonomy_hours = AUTONOMY_HOURS.get(assessment.current_supply_type, 4)
    loads = _summarise_loads(assessment, customer_type, autonomy_hours)

    if assessment.average_monthly_consumption_kwh is None and loads is None:
        result.warnings.append(
            "No consumption recorded — cannot size the system without it. "
            "Add a figure from the customer's bills, or list the devices "
            "they need to power."
        )
        return result

    if assessment.average_monthly_consumption_kwh is not None:
        daily_kwh = float(assessment.average_monthly_consumption_kwh) / 30
        result.consumption_source = "bills"
        if loads and daily_kwh > 0:
            gap = abs(loads.daily_kwh - daily_kwh) / daily_kwh
            if gap > LOAD_VS_BILL_TOLERANCE:
                result.warnings.append(
                    f"Devices listed add up to about {loads.daily_kwh:.1f}kWh/day but the "
                    f"bills suggest {daily_kwh:.1f}kWh/day — sizing used the bills. Check "
                    f"device quantities and hours, or the bill figure."
                )
    else:
        daily_kwh = loads.daily_kwh
        result.consumption_source = "devices"

    result.daily_consumption_kwh = round(daily_kwh, 2)
    if loads:
        result.load_daily_kwh = round(loads.daily_kwh, 2)
        result.diversified_load_kw = round(loads.diversified_kw, 2)
        result.peak_surge_kw = round(loads.peak_surge_kw, 2)

    shading_factor = SHADING_DERATE.get(assessment.shading_level, 1.0)
    effective_sun_hours = PEAK_SUN_HOURS * shading_factor * PERFORMANCE_RATIO
    if effective_sun_hours <= 0:
        result.warnings.append("Shading is too heavy to size a system reliably.")
        return result

    target_kw = daily_kwh / effective_sun_hours
    segment_floor = SEGMENT_MIN_SYSTEM_SIZE_KW.get(customer_type, MIN_SYSTEM_SIZE_KW)
    if target_kw < segment_floor:
        target_kw = segment_floor

    available_area = _available_area(assessment)
    panel, panel_qty = _pick_panel(target_kw, available_area, result.warnings, customer_type)

    # If a fitting panel combo came back smaller than the target (roof
    # constrained), reflect that back into the reported target size so the
    # displayed numbers stay internally consistent.
    if panel and panel_qty:
        achievable_kw = (panel.wattage_w * panel_qty) / 1000
        if achievable_kw < target_kw * 0.95:
            result.warnings.append(
                f"Available roof area limits the system to about "
                f"{achievable_kw:.1f}kW versus the {target_kw:.1f}kW target load "
                f"would ideally call for."
            )
            target_kw = achievable_kw

    result.target_system_size_kw = round(target_kw, 2)
    result.panel = panel
    result.panel_quantity = panel_qty

    min_inverter_kw = target_kw
    if loads:
        min_inverter_kw = max(
            target_kw,
            loads.diversified_kw,
            loads.peak_surge_kw / INVERTER_SURGE_RATIO,
        )
        if (
            loads.has_three_phase
            and assessment.current_supply_type == SiteAssessment.SupplyType.GRID_SINGLE
        ):
            result.warnings.append(
                "Three-phase devices are listed but the supply was recorded as "
                "single phase — confirm the supply, or a phase upgrade is needed."
            )
    result.inverter = _pick_inverter(
        min_inverter_kw,
        assessment,
        result.warnings,
        customer_type,
        needs_three_phase=bool(loads and loads.has_three_phase),
    )

    result.backup_autonomy_hours = autonomy_hours
    if loads and loads.has_essential:
        # Size on the devices that actually have to keep running.
        target_battery_kwh = loads.backup_kwh
    else:
        if loads:
            result.warnings.append(
                "No devices are marked 'keep on during outage' — battery sized "
                "from average consumption instead."
            )
        target_battery_kwh = (daily_kwh / 24) * autonomy_hours
    result.target_battery_capacity_kwh = round(target_battery_kwh, 2)

    battery, battery_qty = _pick_battery(target_battery_kwh, result.warnings)
    result.battery = battery
    result.battery_quantity = battery_qty

    if assessment.structural_integrity_ok is False:
        result.warnings.append(
            "Roof structural integrity was flagged as not sound — resolve "
            "before proceeding to installation regardless of system size."
        )

    return result


def calculate_recommendation(assessment: SiteAssessment) -> SystemRecommendation:
    """Run the calculator and persist the result, creating or replacing
    this assessment's SystemRecommendation."""
    result = compute_sizing(assessment)

    recommendation, _ = SystemRecommendation.objects.update_or_create(
        site_assessment=assessment,
        defaults={
            "daily_consumption_kwh": result.daily_consumption_kwh,
            "target_system_size_kw": result.target_system_size_kw,
            "target_battery_capacity_kwh": result.target_battery_capacity_kwh,
            "backup_autonomy_hours": result.backup_autonomy_hours,
            "panel": result.panel,
            "panel_quantity": result.panel_quantity,
            "inverter": result.inverter,
            "battery": result.battery,
            "battery_quantity": result.battery_quantity,
            "warnings": result.warnings,
            # Recalculating replaces the numbers an engineer may have
            # already signed off on — any prior review no longer applies
            # to what's here now, so it's cleared rather than carried over.
            "is_reviewed": False,
            "reviewed_by": None,
            "reviewed_at": None,
        },
    )
    return recommendation