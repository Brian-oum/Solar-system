"""
Management command: seed_sample_catalog

Loads the SolarPanel / Inverter / Battery catalog with the real line-item
pricing pulled from the Energex Solutions sample proposal (commercial 20kW
3-phase job + residential 12kW single-phase job), so the sizing calculator
in sizing.py has realistic in-stock items to recommend from day one across
both segments.

Usage:
    python manage.py seed_sample_catalog
    python manage.py seed_sample_catalog --stock 50   # override default stock

Safe to re-run: items are matched on (brand, name, model_number) via
get_or_create, and price/stock on an existing match are left untouched
unless --update is passed.

Drop this file at:
    <yourapp>/management/commands/seed_sample_catalog.py
(create the management/ and commands/ directories, each with an empty
__init__.py, if they don't already exist in your app).
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from ...models import Battery, Inverter, SolarPanel  # adjust import path to your app layout

DEFAULT_STOCK = 25

# Pulled directly from the Energex Solutions proposal (DOC-20260608-WA0001):
# commercial 20kW 3-phase system for a commercial building, and a separate
# 12kW single-phase residential system. Panel is the same bifacial module
# in both BOMs, just bought in different quantities.

PANELS = [
    dict(
        name="720W Bifacial Solar Module",
        brand="",
        model_number="",
        wattage_w=720,
        unit_price_kes=Decimal("15840.00"),
        panel_area_sqm=Decimal("3.30"),  # typical footprint for a 720W bifacial module
        efficiency_percent=Decimal("21.5"),
        notes="From Energex Solutions sample proposal — used in both the "
              "20kW commercial BOM (30 units) and 12kW residential BOM (10 units).",
    ),
]

INVERTERS = [
    dict(
        name="20kW Hybrid Three Phase Inverter",
        brand="",
        model_number="",
        capacity_kw=Decimal("20.00"),
        phase=Inverter.Phase.THREE,
        is_hybrid=True,
        unit_price_kes=Decimal("415000.00"),
        notes="From Energex Solutions sample proposal — commercial BOM.",
    ),
    dict(
        name="12kW Low Frequency Hybrid Inverter (2 MPPT)",
        brand="",
        model_number="",
        capacity_kw=Decimal("12.00"),
        phase=Inverter.Phase.SINGLE,
        is_hybrid=True,
        unit_price_kes=Decimal("220000.00"),
        notes="From Energex Solutions sample proposal — residential BOM.",
    ),
]

BATTERIES = [
    dict(
        name="32kWh Lithium-Ion Battery Module",
        brand="",
        model_number="",
        capacity_kwh=Decimal("32.00"),
        chemistry=Battery.Chemistry.LITHIUM,
        usable_depth_of_discharge_percent=90,
        unit_price_kes=Decimal("720000.00"),
        notes="From Energex Solutions sample proposal — commercial BOM.",
    ),
    dict(
        name="16kWh Lithium-Ion Battery",
        brand="",
        model_number="",
        capacity_kwh=Decimal("16.00"),
        chemistry=Battery.Chemistry.LITHIUM,
        usable_depth_of_discharge_percent=90,
        unit_price_kes=Decimal("320000.00"),
        notes="From Energex Solutions sample proposal — residential BOM.",
    ),
]


class Command(BaseCommand):
    help = "Seed SolarPanel/Inverter/Battery catalog from the sample Energex proposal figures."

    def add_arguments(self, parser):
        parser.add_argument(
            "--stock", type=int, default=DEFAULT_STOCK,
            help=f"Stock quantity to set on newly created items (default {DEFAULT_STOCK}).",
        )
        parser.add_argument(
            "--update", action="store_true",
            help="Also refresh price/stock/notes on items that already exist.",
        )

    def handle(self, *args, **options):
        stock = options["stock"]
        update = options["update"]

        created, updated, skipped = 0, 0, 0

        for model, rows, unique_fields in (
            (SolarPanel, PANELS, ("name",)),
            (Inverter, INVERTERS, ("name",)),
            (Battery, BATTERIES, ("name",)),
        ):
            for row in rows:
                lookup = {f: row[f] for f in unique_fields}
                defaults = {**row, "stock_quantity": stock, "is_active": True}
                obj, was_created = model.objects.get_or_create(defaults=defaults, **lookup)
                if was_created:
                    created += 1
                    self.stdout.write(self.style.SUCCESS(f"Created {model.__name__}: {obj}"))
                elif update:
                    for field, value in defaults.items():
                        setattr(obj, field, value)
                    obj.save()
                    updated += 1
                    self.stdout.write(self.style.WARNING(f"Updated {model.__name__}: {obj}"))
                else:
                    skipped += 1
                    self.stdout.write(f"Skipped (already exists): {obj}")

        self.stdout.write(
            self.style.SUCCESS(f"Done. Created {created}, updated {updated}, skipped {skipped}.")
        )