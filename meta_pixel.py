"""
Meta Conversions API — server-side event tracking.

Sends events to Meta regardless of browser ad blockers or iOS privacy restrictions.
Browser pixel events should include a matching event_id for deduplication so Meta
counts each conversion only once.

Required env vars:
  META_PIXEL_ID      — The pixel ID shown in Meta Events Manager
  META_ACCESS_TOKEN  — Conversions API access token from Meta
"""

import os
import time
import uuid
import hashlib
import logging
import requests

logger = logging.getLogger(__name__)

META_PIXEL_ID     = os.environ.get("META_PIXEL_ID", "")
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
GRAPH_API_VERSION = "v18.0"


def get_event_id() -> str:
    """Generate a unique event ID for browser/server deduplication."""
    return str(uuid.uuid4())


def _hash(value: str) -> str:
    """SHA-256 hash a string (Meta requires hashed PII)."""
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def _extract_user_data(request) -> dict:
    """
    Extract user data from a Flask request for Meta Conversions API.
    All PII fields must be SHA-256 hashed per Meta spec.
    """
    user_data = {}

    # Client IP
    client_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or ""
    )
    if client_ip:
        user_data["client_ip_address"] = client_ip

    # User agent
    ua = request.headers.get("User-Agent", "")
    if ua:
        user_data["client_user_agent"] = ua

    # fbc / fbp cookies (Meta click ID and browser ID)
    fbc = request.cookies.get("_fbc", "")
    fbp = request.cookies.get("_fbp", "")
    if fbc:
        user_data["fbc"] = fbc
    if fbp:
        user_data["fbp"] = fbp

    return user_data


def send_event(event_name: str,
               user_data: dict,
               custom_data: dict = None,
               event_id: str = None) -> bool:
    """
    Send a single event to the Meta Conversions API.
    Returns True on success, False on failure.
    """
    if not META_PIXEL_ID or not META_ACCESS_TOKEN:
        logger.warning(
            "[META CAPI] META_PIXEL_ID or META_ACCESS_TOKEN not set — skipping"
        )
        return False

    payload = {
        "data": [
            {
                "event_name":       event_name,
                "event_time":       int(time.time()),
                "event_source_url": "https://morabets.com/dashboard",
                "action_source":    "website",
                "user_data":        user_data,
            }
        ]
    }

    if custom_data:
        payload["data"][0]["custom_data"] = custom_data

    if event_id:
        payload["data"][0]["event_id"] = event_id

    url = (
        f"https://graph.facebook.com"
        f"/{GRAPH_API_VERSION}/{META_PIXEL_ID}/events"
        f"?access_token={META_ACCESS_TOKEN}"
    )

    try:
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code == 200:
            logger.info(f"[META CAPI] {event_name} sent ✅")
            return True
        else:
            logger.warning(
                f"[META CAPI] {event_name} failed "
                f"{resp.status_code}: {resp.text[:200]}"
            )
            return False
    except Exception as e:
        logger.warning(f"[META CAPI] {event_name} exception: {e}")
        return False


def track_lead(request,
               customer_email: str = None,
               event_id: str = None) -> bool:
    """
    Fire a Lead event server-side via Meta Conversions API.
    Matches the browser pixel Lead event for deduplication via event_id.
    """
    user_data = _extract_user_data(request)

    if customer_email:
        user_data["em"] = _hash(customer_email)

    custom_data = {
        "content_name":     "Mora Bets Free Access",
        "content_category": "Sports Betting Tool",
        "value":            14.99,
        "currency":         "USD"
    }

    return send_event("Lead", user_data, custom_data, event_id)


def track_purchase(customer_email: str = None,
                   event_id: str = None,
                   value: float = 7,
                   currency: str = "USD",
                   content_name: str = "Context Edge $7 Trial") -> bool:
    """
    Fire a Purchase event server-side via Meta Conversions API.
    Uses the Stripe Checkout Session ID as event_id for browser/server dedupe.
    """
    user_data = {}

    if customer_email:
        user_data["em"] = _hash(customer_email)

    custom_data = {
        "content_name": content_name,
        "content_category": "Sports Betting Tool",
        "value": value,
        "currency": currency,
    }

    return send_event("Purchase", user_data, custom_data, event_id)
