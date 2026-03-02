# app/app/cal_service.py
from urllib.parse import urlencode

def build_cal_booking_link(contractor: dict, customer_name: str = "", customer_phone: str = "", customer_email: str = "") -> str:
    """
    Returns the contractor-specific Cal booking URL (optionally with prefill query params).
    If no URL exists, returns empty string.
    """
    base = (contractor.get("CAL Booking URL") or "").strip()
    if not base:
        return ""

    # Optional prefill. If Cal ignores any of these, no harm done.
    params = {}
    if customer_name:
        params["name"] = customer_name
    if customer_email:
        params["email"] = customer_email
    if customer_phone:
        params["phone"] = customer_phone

    if not params:
        return base

    joiner = "&" if "?" in base else "?"
    return base + joiner + urlencode(params)
