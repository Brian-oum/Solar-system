"""
Rule-based solar sizing calculator.

Takes a completed (or in-progress) SiteAssessment and produces a
SystemRecommendation: a target system size, target battery capacity, and
the best-fit in-stock catalog items for each.

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
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import Battery, Inverter, SiteAssessment, SolarPanel, SystemRecommendation

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
    warnings: list[str] = field(default_factory=list)


def _available_area(assessment: SiteAssessment) -> float | None:
    """Prefer the explicit installable area; fall back to total roof area."""
    if assessment.available_installation_area_sqm is not None:
        return float(assessment.available_installation_area_sqm)
    if assessment.roof_area_sqm is not None:
        return float(assessment.roof_area_sqm)
    return None


def _pick_panel(target_kw: float, available_area_sqm: float | None, warnings: list[str]):
    """
    Among in-stock active panels, choose the one that hits the target kW
    within the available roof area at the lowest total cost. Falls back to
    whatever panel needs the fewest panels if nothing fits the area, and
    flags that with a warning rather than silently oversizing the roof.
    """
    candidates = SolarPanel.objects.filter(is_active=True, stock_quantity__gt=0)
    if not candidates.exists():
        warnings.append("No active, in-stock solar panels in the catalog.")
        return None, 0

    best_fit = None
    best_fit_cost = None
    best_overall = None
    best_overall_count = None

    for panel in candidates:
        count = max(1, math.ceil((target_kw * 1000) / panel.wattage_w))
        count = min(count, panel.stock_quantity)
        if count * panel.wattage_w < target_kw * 1000 * 0.95:
            # Even using all stock of this panel, it can't cover the target —
            # skip it as a fit candidate (still tracked as "best overall" below).
            fits_target = False
        else:
            fits_target = True

        area_needed = count * float(panel.panel_area_sqm or DEFAULT_PANEL_AREA_SQM)
        fits_area = available_area_sqm is None or area_needed <= available_area_sqm

        cost = float(panel.unit_price_kes or 0) * count

        if fits_target and fits_area:
            if best_fit is None or cost < best_fit_cost:
                best_fit, best_fit_cost = panel, cost

        # Track the smallest-panel-count fallback in case nothing fits area.
        if best_overall is None or count < best_overall_count:
            best_overall, best_overall_count = panel, count

    if best_fit is not None:
        count = max(1, math.ceil((target_kw * 1000) / best_fit.wattage_w))
        return best_fit, min(count, best_fit.stock_quantity)

    if best_overall is not None:
        warnings.append(
            "No in-stock panel combination fits the available roof area at the "
            "target size — showing the closest option; roof area may be a "
            "hard constraint here."
        )
        return best_overall, best_overall_count

    return None, 0


def _pick_inverter(target_kw: float, assessment: SiteAssessment, warnings: list[str]):
    wants_three_phase = assessment.current_supply_type == SiteAssessment.SupplyType.GRID_THREE
    wants_hybrid = assessment.current_supply_type in (
        SiteAssessment.SupplyType.OFF_GRID,
        SiteAssessment.SupplyType.GENERATOR,
    ) or not assessment.has_existing_backup

    candidates = Inverter.objects.filter(is_active=True, stock_quantity__gt=0)
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
            f"No in-stock inverter rated ≥{target_kw:.1f}kW — "
            f"largest available is {largest.capacity_kw}kW."
        )
        return largest

    warnings.append("No active, in-stock inverters in the catalog.")
    return None


def _pick_battery(target_kwh: float, warnings: list[str]):
    candidates = Battery.objects.filter(is_active=True, stock_quantity__gt=0)
    if not candidates.exists():
        warnings.append("No active, in-stock batteries in the catalog.")
        return None, 0

    best = None
    best_count = None
    best_cost = None
    for battery in candidates:
        usable_kwh = float(battery.capacity_kwh) * (battery.usable_depth_of_discharge_percent / 100)
        if usable_kwh <= 0:
            continue
        count = max(1, math.ceil(target_kwh / usable_kwh))
        count = min(count, battery.stock_quantity)
        cost = float(battery.unit_price_kes or 0) * count
        if best is None or cost < best_cost:
            best, best_count, best_cost = battery, count, cost

    if best is None:
        warnings.append("No suitable battery configuration found in stock.")
        return None, 0
    return best, best_count


def compute_sizing(assessment: SiteAssessment) -> SizingResult:
    result = SizingResult()

    if assessment.average_monthly_consumption_kwh is None:
        result.warnings.append(
            "No average monthly consumption recorded — cannot size the system "
            "without it. Add a figure from the customer's bills."
        )
        return result

    daily_kwh = float(assessment.average_monthly_consumption_kwh) / 30
    result.daily_consumption_kwh = round(daily_kwh, 2)

    shading_factor = SHADING_DERATE.get(assessment.shading_level, 1.0)
    effective_sun_hours = PEAK_SUN_HOURS * shading_factor * PERFORMANCE_RATIO
    if effective_sun_hours <= 0:
        result.warnings.append("Shading is too heavy to size a system reliably.")
        return result

    target_kw = daily_kwh / effective_sun_hours
    target_kw = max(target_kw, MIN_SYSTEM_SIZE_KW)

    available_area = _available_area(assessment)
    panel, panel_qty = _pick_panel(target_kw, available_area, result.warnings)

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

    result.inverter = _pick_inverter(target_kw, assessment, result.warnings)

    autonomy_hours = AUTONOMY_HOURS.get(assessment.current_supply_type, 4)
    result.backup_autonomy_hours = autonomy_hours
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
        },
    )
    return recommendation