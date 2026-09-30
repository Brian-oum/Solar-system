"""
Suggested devices to capture during a site survey, by customer classification.

This is plain data, not logic — the same "named, adjustable, no code changes
needed" approach as the constants in sizing.py. Edit a row to change a
default wattage, add a row to make a new device appear on the survey, or
delete one to stop suggesting it. Nothing here is stored in the database;
the survey form pre-fills these as zero-quantity rows and only the devices
the engineer actually sets a quantity for get saved.

Row format:
    (category, name, rated_watts, surge_multiplier, hours_per_day,
     essential_for_backup, three_phase)

- rated_watts: running power of ONE unit.
- surge_multiplier: start-up power as a multiple of running power
  (1.0 = no surge; motors/compressors are typically 3-6x).
- hours_per_day: realistic *effective* hours at that power. For cycling
  loads (fridges, AC compressors) enter the equivalent full-power hours.
- essential_for_backup: pre-ticked "must stay on during an outage".
- three_phase: the device needs a three-phase supply.
"""
from __future__ import annotations

from decimal import Decimal

from .models import Customer, SiteLoadItem

C = SiteLoadItem.Category

RESIDENTIAL = [
    (C.LIGHTING, "LED bulb", 9, 1.0, 5, True, False),
    (C.LIGHTING, "Security / outdoor light", 20, 1.0, 10, True, False),
    (C.REFRIGERATION, "Fridge / freezer", 150, 4.0, 10, True, False),
    (C.ENTERTAINMENT_IT, "Television", 100, 1.0, 5, False, False),
    (C.ENTERTAINMENT_IT, "Decoder / set-top box", 20, 1.0, 5, False, False),
    (C.ENTERTAINMENT_IT, "Wi-Fi router", 12, 1.0, 24, True, False),
    (C.ENTERTAINMENT_IT, "Laptop / desktop", 65, 1.0, 6, False, False),
    (C.ENTERTAINMENT_IT, "Phone / device charging", 10, 1.0, 4, True, False),
    (C.KITCHEN, "Microwave", 1200, 1.0, 0.3, False, False),
    (C.KITCHEN, "Electric kettle", 1800, 1.0, 0.3, False, False),
    (C.KITCHEN, "Electric cooker / oven", 3000, 1.0, 1.5, False, False),
    (C.KITCHEN, "Blender / food processor", 400, 2.0, 0.2, False, False),
    (C.LAUNDRY, "Washing machine", 500, 3.0, 1, False, False),
    (C.LAUNDRY, "Electric iron", 1000, 1.0, 0.5, False, False),
    (C.WATER, "Water pump", 750, 4.0, 1.5, True, False),
    (C.WATER, "Water heater / shower", 3000, 1.0, 1, False, False),
    (C.COOLING_HEATING, "Ceiling / standing fan", 60, 1.5, 6, False, False),
    (C.COOLING_HEATING, "Air conditioner", 1200, 3.0, 6, False, False),
    (C.SECURITY_COMMS, "CCTV / alarm system", 60, 1.0, 24, True, False),
    (C.SECURITY_COMMS, "Electric fence energiser", 15, 1.0, 24, True, False),
]

COMMERCIAL = [
    (C.LIGHTING, "LED light fitting", 18, 1.0, 10, True, False),
    (C.LIGHTING, "Security / signage light", 40, 1.0, 12, True, False),
    (C.OFFICE_POS, "Desktop computer + monitor", 150, 1.0, 9, True, False),
    (C.OFFICE_POS, "Laptop", 65, 1.0, 9, True, False),
    (C.OFFICE_POS, "POS terminal / receipt printer", 60, 1.0, 12, True, False),
    (C.OFFICE_POS, "Printer / photocopier", 800, 2.0, 1, False, False),
    (C.ENTERTAINMENT_IT, "Server / network rack", 500, 1.0, 24, True, False),
    (C.ENTERTAINMENT_IT, "Wi-Fi router / switch", 25, 1.0, 24, True, False),
    (C.REFRIGERATION, "Display fridge / chiller", 400, 4.0, 12, True, False),
    (C.REFRIGERATION, "Chest freezer", 300, 4.0, 12, True, False),
    (C.REFRIGERATION, "Walk-in cold room", 3500, 4.0, 12, True, False),
    (C.COOLING_HEATING, "Split air conditioner (18,000 BTU)", 1800, 3.0, 8, False, False),
    (C.COOLING_HEATING, "Ceiling / extraction fan", 75, 1.5, 10, False, False),
    (C.KITCHEN, "Commercial oven / cooker", 5000, 1.0, 4, False, False),
    (C.KITCHEN, "Water dispenser / kettle", 1500, 1.0, 1, False, False),
    (C.WATER, "Water pump", 1100, 4.0, 2, True, False),
    (C.MOTORS_MACHINERY, "Lift / elevator", 7500, 3.0, 2, False, True),
    (C.SECURITY_COMMS, "CCTV / access control", 120, 1.0, 24, True, False),
    (C.SECURITY_COMMS, "Electric fence energiser", 15, 1.0, 24, True, False),
]

INDUSTRIAL = [
    (C.LIGHTING, "High-bay LED light", 150, 1.0, 12, True, False),
    (C.LIGHTING, "Yard / flood light", 100, 1.0, 12, True, False),
    (C.MOTORS_MACHINERY, "Three-phase motor (5.5 kW)", 5500, 6.0, 8, False, True),
    (C.MOTORS_MACHINERY, "Three-phase motor (11 kW)", 11000, 6.0, 8, False, True),
    (C.MOTORS_MACHINERY, "Air compressor", 7500, 5.0, 8, False, True),
    (C.MOTORS_MACHINERY, "Conveyor", 3000, 4.0, 10, False, True),
    (C.MOTORS_MACHINERY, "Welding machine", 6000, 1.5, 3, False, True),
    (C.MOTORS_MACHINERY, "Milling / grinding / cutting machine", 4000, 5.0, 6, False, True),
    (C.MOTORS_MACHINERY, "Industrial mixer", 5500, 5.0, 6, False, True),
    (C.WATER, "Borehole / process pump", 3700, 5.0, 6, True, True),
    (C.REFRIGERATION, "Cold room / industrial chiller", 5000, 4.0, 14, True, True),
    (C.COOLING_HEATING, "Industrial air conditioner / HVAC", 9000, 3.0, 8, False, True),
    (C.COOLING_HEATING, "Extraction / ventilation fan", 750, 3.0, 10, False, True),
    (C.OFFICE_POS, "Office equipment (per workstation)", 200, 1.0, 9, True, False),
    (C.ENTERTAINMENT_IT, "Server / control system", 800, 1.0, 24, True, False),
    (C.SECURITY_COMMS, "CCTV / access control", 200, 1.0, 24, True, False),
]

PRESETS_BY_CUSTOMER_TYPE = {
    Customer.CustomerType.RESIDENTIAL: RESIDENTIAL,
    Customer.CustomerType.COMMERCIAL: COMMERCIAL,
    Customer.CustomerType.INDUSTRIAL: INDUSTRIAL,
}


def preset_rows(customer_type):
    """Suggested devices for a classification, as form-initial dicts."""
    rows = PRESETS_BY_CUSTOMER_TYPE.get(customer_type, RESIDENTIAL)
    return [
        {
            "category": category,
            "name": name,
            "quantity": 0,
            "rated_power_w": watts,
            "surge_multiplier": Decimal(str(surge)),
            "hours_per_day": Decimal(str(hours)),
            "is_essential": essential,
            "is_three_phase": three_phase,
        }
        for category, name, watts, surge, hours, essential, three_phase in rows
    ]