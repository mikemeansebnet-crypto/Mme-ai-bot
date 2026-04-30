# -----------------------------------------------
# FILE: app/app/stripe_service.py
# What it does: Creates Stripe payment links for
# completed jobs and handles payment webhooks
# to update Airtable when customers pay
# -----------------------------------------------

import os
import stripe
import requests
import threading
import time

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_PAYMENTS_TABLE = "Payments"
AIRTABLE_CONTRACTORS_TABLE = os.environ.get("AIRTABLE_CONTRACTORS_TABLE")

PAYMENTS_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_PAYMENTS_TABLE}"
CONTRACTORS_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_CONTRACTORS_TABLE}"
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


def fetch_payment_record(record_id: str) -> dict:
    """Fetches full payment record from Airtable to get customer and contractor details."""
    try:
        response = requests.get(
            f"{PAYMENTS_URL}/{record_id}",
            headers=HEADERS
        )
        if response.status_code == 200:
            return response.json().get("fields", {})
        print(f"FETCH PAYMENT RECORD FAILED | {record_id} | {response.status_code}")
        return {}
    except Exception as e:
        print(f"FETCH PAYMENT RECORD ERROR | {e}")
        return {}


def fetch_contractor_by_twilio(twilio_number: str) -> dict:
    """Fetches contractor record from Airtable by Twilio number."""
    try:
        params = {"filterByFormula": f"{{Twilio Number}} = '{twilio_number}'"}
        response = requests.get(CONTRACTORS_URL, headers=HEADERS, params=params)
        records = response.json().get("records", [])
        if records:
            return records[0].get("fields", {})
        return {}
    except Exception as e:
        print(f"FETCH CONTRACTOR ERROR | {e}")
        return {}


def send_followup_sms(to_number: str, body: str, from_number: str) -> None:
    """Sends an SMS via Twilio."""
    try:
        from twilio.rest import Client
        client = Client(
            os.environ.get("TWILIO_ACCOUNT_SID"),
            os.environ.get("TWILIO_AUTH_TOKEN")
        )
        client.messages.create(
            body=body,
            from_=from_number,
            to=to_number
        )
        print(f"FOLLOWUP SMS SENT | {to_number} | {body[:50]}...")
    except Exception as e:
        print(f"FOLLOWUP SMS ERROR | {e}")


def schedule_followup_messages(record_id: str) -> None:
    """
    Fetches payment record and schedules two follow-up messages:
    - Message 1 (referral): 30 minutes after payment confirmed
    - Message 2 (review request): 24 hours after payment confirmed
    Runs in a background thread so it doesn't block the webhook response.
    """
    def run():
        try:
            # Fetch payment record to get customer info and linked contractor
            fields = fetch_payment_record(record_id)
            if not fields:
                print(f"FOLLOWUP | No payment fields found for {record_id}")
                return

            customer_name = fields.get("Customer Name", "there")
            customer_phone = fields.get("Phone Number", "")
            first_name = customer_name.split()[0] if customer_name else "there"

            # Get linked contractor record ID
            contractor_links = fields.get("Contractor", [])
            contractor_record_id = contractor_links[0] if contractor_links else None

            if not contractor_record_id:
                print(f"FOLLOWUP | No linked contractor for payment {record_id}")
                return

            # Fetch contractor details
            contractor_response = requests.get(
                f"{CONTRACTORS_URL}/{contractor_record_id}",
                headers=HEADERS
            )
            if contractor_response.status_code != 200:
                print(f"FOLLOWUP | Could not fetch contractor {contractor_record_id}")
                return

            contractor = contractor_response.json().get("fields", {})
            twilio_number = contractor.get("Twilio Number", "")
            business_name = contractor.get("Business Name", "your contractor")
            referral_message = contractor.get("Referral Message", "")
            review_link = contractor.get("Review Link", "")

            if not customer_phone or not twilio_number:
                print(f"FOLLOWUP | Missing phone numbers | customer:{customer_phone} twilio:{twilio_number}")
                return

            # Message 1 — Referral (30 minutes)
            if referral_message:
                time.sleep(30 * 60)  # 30 minutes
                referral_body = (
                    f"Hi {first_name}! Thank you for choosing {business_name}. "
                    f"{referral_message}"
                )
                send_followup_sms(customer_phone, referral_body, twilio_number)
                print(f"FOLLOWUP REFERRAL SENT | {customer_phone}")
            else:
                print(f"FOLLOWUP | No referral message set for contractor {contractor_record_id}")

            # Message 2 — Review request (24 hours from payment, so ~23.5 hours after referral)
            if review_link:
                time.sleep(23 * 60 * 60 + 30 * 60)  # 23.5 more hours
                review_body = (
                    f"Hi {first_name}! We hope you're loving the results. "
                    f"Would you mind leaving us a quick review? It means the world to us: "
                    f"{review_link}"
                )
                send_followup_sms(customer_phone, review_body, twilio_number)
                print(f"FOLLOWUP REVIEW SENT | {customer_phone}")
            else:
                print(f"FOLLOWUP | No review link set for contractor {contractor_record_id}")

        except Exception as e:
            print(f"FOLLOWUP THREAD ERROR | {type(e).__name__} | {e}")

    # Run in background thread so webhook returns immediately
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    print(f"FOLLOWUP SCHEDULED | {record_id} | referral: 30min | review: 24hr")


def update_airtable_paid(record_id: str) -> None:
    """Updates Airtable payment status to Paid and triggers follow-up messages."""
    try:
        response = requests.patch(
            f"{PAYMENTS_URL}/{record_id}",
            headers=HEADERS,
            json={"fields": {"Payment Status": "Paid"}}
        )
        if response.status_code != 200:
            print(f"AIRTABLE UPDATE FAILED | {record_id} | {response.status_code} | {response.text}")
        else:
            print(f"AIRTABLE PAYMENT STATUS UPDATED | {record_id} | Paid")
            # Trigger follow-up messages in background
            schedule_followup_messages(record_id)
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
            print(f"STRIPE EVENT UNHANDLED | {event_type}")

        return {"ok": True}

    except Exception as e:
        print(f"STRIPE EVENT HANDLER ERROR | {type(e).__name__} | {e}")
        return {"ok": False, "error": str(e)}


# --------------------------------------------------
# DEPRECATED — do not call this directly anymore.
# --------------------------------------------------
def handle_stripe_webhook(payload: bytes, sig_header: str) -> dict:
    """DEPRECATED: Use handle_stripe_event(event) instead."""
    print("WARNING | handle_stripe_webhook called directly — this is deprecated.")
    return {"ok": False, "error": "Use handle_stripe_event with a pre-verified event."}
        
