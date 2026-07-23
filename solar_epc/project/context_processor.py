WORKFLOW_PHASES = [
    "Client registration",
    "Site assessment",
    "Load analysis",
    "System sizing",
    "Recommendation engine",
    "Engineer review",
    "Quotation",
    "Client approval",
    "Invoicing",
    "Payment tracking",
    "Inventory reservation",
    "Project scheduling",
    "Installation",
    "Testing & commissioning",
    "Handover",
    "Warranty & maintenance",
]

# Phases already live in the product. Everything else renders in the
# sidebar's "Full lifecycle" roadmap as upcoming, so the nav itself
# communicates how much of the EPC workflow is built so far.
ACTIVE_PHASES = {1}


def workflow_roadmap(request):
    if not request.user.is_authenticated:
        return {}
    return {
        "workflow_roadmap": [
            {"n": i, "label": label, "active": i in ACTIVE_PHASES}
            for i, label in enumerate(WORKFLOW_PHASES, start=1)
        ]
    }