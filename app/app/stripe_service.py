# -----------------------------------------------
# FILE: app/app/stripe_service.py
# What it does: Creates Stripe payment links for
# completed jobs and handles payment webhooks
# to update Airtable when customers pay
# -----------------------------------------------

import os
import stripe
import requests

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_PAYMENTS_TABLE = "Payments"

PAYMENTS_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_PAYMENTS_TABLE}"
HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}


def create_payment_link(amount: float, customer_name: str, job_description: str, record_id: str, business_name: str = "Your Contractor") -> dict:
    """
    Creates a Stripe payment link for a completed job.
    Returns the payment link URL.
    """
    try:
        amount_cents = int(float(amount) * 100)

        price = stripe.Price.create(
            currency="usd",
            unit_amount=amount_cents,
            product_data={
                # FIXED: Uses contractor's business name so customer recognizes who is charging them
                "name": f"{business_name} - {job_description or 'Service Payment'}",
            },
        )

        payment_link = stripe.PaymentLink.create(
            line_items=[{"price": price.id, "quantity": 1}],
            metadata={
                "airtable_record_id": record_id,
                "customer_name": customer_name,
            },
            after_completion={
                "type": "redirect",
                "redirect": {"url": os.environ.get("APP_BASE_URL", "https://mme-ai-bot.onrender.com") + "/payment-success"}
            }
        )

        print(f"STRIPE PAYMENT LINK CREATED | {business_name} | {customer_name} | ${amount} | {payment_link.url}")
        return {"ok": True, "url": payment_link.url, "link_id": payment_link.id}

    except Exception as e:
        print(f"STRIPE PAYMENT LINK ERROR | {e}")
        return {"ok": False, "error": str(e)}


def update_airtable_paid(record_id: str) -> None:
    """Updates Airtable payment status to Paid when Stripe confirms payment."""
    try:
        response = requests.patch(
            f"{PAYMENTS_URL}/{record_id}",
            headers=HEADERS,
            json={"fields": {"Payment Status": "Paid"}}
        )
        # FIXED: Check response so silent Airtable failures get logged
        if response.status_code != 200:
            print(f"AIRTABLE UPDATE FAILED | {record_id} | {response.status_code} | {response.text}")
        else:
            print(f"AIRTABLE PAYMENT STATUS UPDATED | {record_id} | Paid")
    except Exception as e:
        print(f"AIRTABLE UPDATE ERROR | {e}")


def handle_stripe_event(event: dict) -> dict:
    """
    Processes an already-verified Stripe event.
    Signature verification happens ONCE in the webhook route — not here.
    Handles payment confirmation events and updates Airtable accordingly.
    """
    try:
        event_type = event["type"]
        obj = event["data"]["object"]

        # Helper to safely pull metadata from either dict or object
        def get_record_id(obj):
            metadata = getattr(obj, "metadata", None) or obj.get("metadata", {})
            if isinstance(metadata, dict):
                return metadata.get("airtable_record_id")
            return getattr(metadata, "airtable_record_id", None)

        if event_type == "checkout.session.completed":
            record_id = get_record_id(obj)
            if record_id:
                update_airtable_paid(record_id)
                print(f"PAYMENT CONFIRMED | checkout.session.completed | Record: {record_id}")
            else:
                print(f"PAYMENT CONFIRMED | checkout.session.completed | No record_id in metadata")

        elif event_type == "payment_intent.succeeded":
            record_id = get_record_id(obj)
            if record_id:
                update_airtable_paid(record_id)
                print(f"PAYMENT CONFIRMED | payment_intent.succeeded | Record: {record_id}")
            else:
                print(f"PAYMENT CONFIRMED | payment_intent.succeeded | No record_id in metadata")

        elif event_type == "payment_link.completed":
            record_id = get_record_id(obj)
            if record_id:
                update_airtable_paid(record_id)
                print(f"PAYMENT CONFIRMED | payment_link.completed | Record: {record_id}")
            else:
                print(f"PAYMENT CONFIRMED | payment_link.completed | No record_id in metadata")

        else:
            # Log unhandled event types so you can add handlers as needed
            print(f"STRIPE EVENT UNHANDLED | {event_type}")

        return {"ok": True}

    except Exception as e:
        print(f"STRIPE EVENT HANDLER ERROR | {type(e).__name__} | {e}")
        return {"ok": False, "error": str(e)}


# --------------------------------------------------
# DEPRECATED — do not call this directly anymore.
# Signature verification now handled in the route.
# Kept here only for reference during transition.
# --------------------------------------------------
def handle_stripe_webhook(payload: bytes, sig_header: str) -> dict:
    """
    DEPRECATED: Use handle_stripe_event(event) instead.
    Verification now happens once in the /stripe-webhook route.
    """
    print("WARNING | handle_stripe_webhook called directly — this is deprecated.")
    return {"ok": False, "error": "Use handle_stripe_event with a pre-verified event."}
        
