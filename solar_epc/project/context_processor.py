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
#   1 Client registration, 2 Site assessment — their own build-out.
#   3-5 (Load analysis / System sizing / Recommendation engine) are one
#   feature in practice: sizing.py runs off the site assessment survey.
#   6 Engineer review — the SystemRecommendation sign-off gate, before a
#   project can move into Quotation.
#   7 Quotation, 8 Client approval — the quotation_* views/actions.
ACTIVE_PHASES = {1, 2, 3, 4, 5, 6, 7, 8}


def workflow_roadmap(request):
    if not request.user.is_authenticated:
        return {}
    return {
        "workflow_roadmap": [
            {"n": i, "label": label, "active": i in ACTIVE_PHASES}
            for i, label in enumerate(WORKFLOW_PHASES, start=1)
        ]
    }