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


def create_payment_link(amount: float, customer_name: str, job_description: str, record_id: str) -> dict:
    """
    Creates a Stripe payment link for a completed job.
    Returns the payment link URL.
    """
    try:
        # Convert amount to cents for Stripe
        amount_cents = int(float(amount) * 100)

        # Create a Stripe price on the fly
        price = stripe.Price.create(
            currency="usd",
            unit_amount=amount_cents,
            product_data={
                "name": f"MME Lawn Care - {job_description or 'Service Payment'}",
            },
        )

        # Create payment link
        payment_link = stripe.PaymentLink.create(
            line_items=[{"price": price.id, "quantity": 1}],
            metadata={
                "airtable_record_id": record_id,
                "customer_name": customer_name,
            },
            after_completion={
                "type": "redirect",
                "redirect": {"url": "https://mme-ai-bot.onrender.com/payment-success"}
            }
        )

        print(f"STRIPE PAYMENT LINK CREATED | {customer_name} | ${amount} | {payment_link.url}")
        return {"ok": True, "url": payment_link.url, "link_id": payment_link.id}

    except Exception as e:
        print(f"STRIPE PAYMENT LINK ERROR | {e}")
        return {"ok": False, "error": str(e)}


def update_airtable_paid(record_id: str) -> None:
    """Updates Airtable payment status to Paid when Stripe confirms payment."""
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError as e:
        print(f"STRIPE WEBHOOK SIGNATURE ERROR | {e}")
        return {"ok": False, "error": "Invalid signature"}

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        record_id = session.get("metadata", {}).get("airtable_record_id")
        if record_id:
            update_airtable_paid(record_id)
            print(f"PAYMENT CONFIRMED | Record: {record_id}")

    elif event["type"] == "payment_intent.succeeded":
        session = event["data"]["object"]
        record_id = session.get("metadata", {}).get("airtable_record_id")
        if record_id:
            update_airtable_paid(record_id)
            print(f"PAYMENT INTENT CONFIRMED | Record: {record_id}")

    elif event["type"] == "payment_link.completed":
        link = event["data"]["object"]
        record_id = link.get("metadata", {}).get("airtable_record_id")
        if record_id:
            update_airtable_paid(record_id)

    return {"ok": True}
        requests.patch(
            f"{PAYMENTS_URL}/{record_id}",
            headers=HEADERS,
            json={"fields": {"Payment Status": "Paid"}}
        )
        print(f"AIRTABLE PAYMENT STATUS UPDATED | {record_id} | Paid")
    except Exception as e:
        print(f"AIRTABLE UPDATE ERROR | {e}")


def handle_stripe_webhook(payload: bytes, sig_header: str) -> dict:
    """
    Verifies and processes Stripe webhook events.
    Called from the /stripe-webhook Flask route.
    """
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")

    try:
        
