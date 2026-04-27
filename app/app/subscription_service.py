# -----------------------------------------------
# FILE: app/app/subscription_service.py
# What it does: Checks contractor subscription tier
# and blocks features based on plan level
# -----------------------------------------------

# Feature access by tier
TIER_FEATURES = {
    "Basic": [
        "sms_intake",
        "lead_followup",
        "cal_booking",
        "payment_notifications",
        "cancel_reschedule",
    ],
    "Pro": [
        "sms_intake",
        "lead_followup",
        "cal_booking",
        "payment_notifications",
        "cancel_reschedule",
        "voice_intake",
        "photo_estimates",
        "stripe_payments",
    ],
    "Trial": [
        "sms_intake",
        "lead_followup",
        "cal_booking",
        "cancel_reschedule",
    ],
}

# SMS messages sent to customers when feature is blocked
UPGRADE_MESSAGES = {
    "voice_intake": (
        "Thanks for calling! Our phone intake is available on the Pro plan. "
        "Please text us instead."
    ),
    "photo_estimates": (
        "Photo estimates are available on the Pro plan. "
        "Reply with your job details and we'll get back to you."
    ),
    "stripe_payments": (
        "Online card payments are available on the Pro plan. "
        "Please arrange payment directly with your contractor."
    ),
}


def get_contractor_tier(contractor: dict) -> str:
    """Returns the contractor's subscription tier. Defaults to Basic."""
    return (contractor.get("Subscription Tier") or "Basic").strip()


def get_contractor_status(contractor: dict) -> str:
    """Returns the contractor's subscription status."""
    return (contractor.get("Subscription Status") or "").strip()


def is_subscription_active(contractor: dict) -> bool:
    """Returns True if contractor has an active subscription."""
    status = get_contractor_status(contractor)
    return status in ["Active"]


def has_feature(contractor: dict, feature: str) -> bool:
    """
    Checks if a contractor has access to a specific feature
    based on their subscription tier and status.
    """
    # Always allow if subscription is active
    if not is_subscription_active(contractor):
        return False

    tier = get_contractor_tier(contractor)
    allowed_features = TIER_FEATURES.get(tier, TIER_FEATURES["Basic"])
    return feature in allowed_features


def get_upgrade_message(feature: str) -> str:
    """Returns a customer-facing message when a feature is not available."""
    return UPGRADE_MESSAGES.get(
        feature,
        "This feature is not available on your current plan. Please contact your contractor."
    )
