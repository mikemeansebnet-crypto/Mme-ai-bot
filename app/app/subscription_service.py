# -----------------------------------------------
# FILE: app/app/subscription_service.py
# What it does: Checks contractor subscription tier
# and blocks features based on plan level
# -----------------------------------------------

TIER_FEATURES = {
    "Basic": [
        "sms_intake",
        "voice_intake",
        "lead_followup",
        "cal_booking",
        "payment_notifications",
        "cancel_reschedule",
        "stripe_payments",
    ],
    "Pro": [
        "sms_intake",
        "voice_intake",
        "lead_followup",
        "cal_booking",
        "payment_notifications",
        "cancel_reschedule",
        "stripe_payments",
        "photo_estimates",
    ],
    "Trial": [
        "sms_intake",
        "lead_followup",
        "cal_booking",
    ],
}

# SMS messages sent to customers when feature is blocked
UPGRADE_MESSAGES = {
    "voice_intake": (
        "Thanks for calling! Phone intake isn't available right now. "
        "Please text us and we'll get back to you shortly."
    ),
    "photo_estimates": (
        "Photo estimates aren't available right now. "
        "Reply with your job details and we'll get back to you."
    ),
    "stripe_payments": (
        "Online payments aren't available right now. "
        "Please contact us to arrange payment."
    ),
}

# Internal alerts sent to contractor when their account has an issue
CONTRACTOR_ALERTS = {
    "canceled": (
        "Your CrewCachePro subscription is inactive. "
        "Log in to reactivate and restore your account features."
    ),
    "past_due": (
        "Your CrewCachePro payment is past due. "
        "Please update your billing to avoid losing access."
    ),
}


def get_contractor_tier(contractor: dict) -> str:
    """Returns the contractor's subscription tier. Defaults to Basic."""
    return (contractor.get("Subscription Tier") or "Basic").strip()


def get_contractor_status(contractor: dict) -> str:
    """Returns the contractor's subscription status, lowercased for safe comparison."""
    return (contractor.get("Subscription Status") or "").strip().lower()


def is_subscription_active(contractor: dict) -> bool:
    """
    Returns True if contractor has an active or grace-period subscription.
    - active: fully paid and current
    - trialing: free trial period
    - past_due: payment failed but still in Stripe grace period (typically 3-7 days)
    """
    status = get_contractor_status(contractor)
    return status in ["active", "trialing", "past_due"]


def is_subscription_canceled(contractor: dict) -> bool:
    """Returns True if the contractor's subscription has been canceled or deactivated."""
    status = get_contractor_status(contractor)
    return status in ["canceled", "inactive", "unpaid"]


def has_feature(contractor: dict, feature: str) -> bool:
    """
    Checks if a contractor has access to a specific feature
    based on their subscription tier and status.
    """
    if not is_subscription_active(contractor):
        return False
    tier = get_contractor_tier(contractor)
    allowed_features = TIER_FEATURES.get(tier, TIER_FEATURES["Basic"])
    return feature in allowed_features


def get_upgrade_message(feature: str) -> str:
    """Returns a customer-facing message when a feature is not available."""
    return UPGRADE_MESSAGES.get(
        feature,
        "This feature isn't available right now. Please contact us directly."
    )


def get_contractor_alert(contractor: dict) -> str | None:
    """
    Returns an internal alert message to send to the contractor
    if their account status needs attention. Returns None if account is healthy.
    """
    status = get_contractor_status(contractor)
    return CONTRACTOR_ALERTS.get(status, None)

def handle_subscription_event(event: dict) -> dict:
    from app.app.contractor_onboarding import handle_subscription_event as _handle
    # FIXED: Convert Stripe object to dict before passing
    if hasattr(event, 'to_dict'):
        event = event.to_dict()
    return _handle(event)
