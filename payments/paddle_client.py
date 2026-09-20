"""Paddle API client and webhook signature verification."""

import hashlib
import hmac
import json
import logging
import os
import requests

logger = logging.getLogger(__name__)

PADDLE_SANDBOX_URL = "https://sandbox-api.paddle.com"
PADDLE_PRODUCTION_URL = "https://api.paddle.com"


def get_environment():
    return os.getenv("PADDLE_ENVIRONMENT", "sandbox").strip().lower()


def get_base_url():
    return PADDLE_PRODUCTION_URL if get_environment() == "production" else PADDLE_SANDBOX_URL


def get_paddle_config():
    """Public Paddle configuration for the frontend checkout."""
    return {
        "environment": get_environment(),
        "client_token": os.getenv("PADDLE_CLIENT_TOKEN", "").strip(),
        "price_id": os.getenv("PADDLE_PRICE_ID", "").strip(),
        "vendor_id": os.getenv("PADDLE_VENDOR_ID", "").strip(),
        "price_amount": "0.99",
        "currency": "USD",
        "plan_name": "Unlimited Lifetime Access",
        "max_free_requests": int(os.getenv("MAX_FREE_PAID_REQUESTS", "50")),
    }


def verify_paddle_signature(raw_body: bytes, signature_header: str, secret: str = None) -> bool:
    """Verify Paddle Billing V2 webhook signature.

    Header format: `ts=1690000000;h1=hash1;h1=hash2`
    The signed payload is `{ts}:{raw_body}` signed with HMAC-SHA256.
    """
    secret = secret or os.getenv("PADDLE_WEBHOOK_SECRET", "").strip()

    # If no secret is configured, allow in development/testing mode
    if not secret:
        is_dev = os.getenv("DEVELOPMENT_MODE", "False") == "True" or os.getenv("DEBUG", "False") == "True"
        if is_dev:
            logger.warning("PADDLE_WEBHOOK_SECRET not set; allowing webhook in development mode.")
            return True
        logger.error("PADDLE_WEBHOOK_SECRET not set; rejecting webhook.")
        return False

    if not signature_header:
        return False

    try:
        parts = {}
        h1_hashes = []
        for segment in signature_header.split(";"):
            if "=" in segment:
                key, val = segment.strip().split("=", 1)
                if key == "h1":
                    h1_hashes.append(val)
                else:
                    parts[key] = val

        ts = parts.get("ts")
        if not ts or not h1_hashes:
            return False

        if isinstance(raw_body, str):
            raw_body = raw_body.encode("utf-8")

        signed_payload = f"{ts}:".encode("utf-8") + raw_body
        expected_hash = hmac.new(
            secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        return any(hmac.compare_digest(expected_hash, candidate) for candidate in h1_hashes)
    except Exception as exc:
        logger.error(f"Error validating Paddle signature: {exc}")
        return False


def fetch_transaction(transaction_id: str) -> dict | None:
    """Fetch transaction details from Paddle Billing REST API."""
    api_key = os.getenv("PADDLE_API_KEY", "").strip()
    if not api_key:
        return None

    url = f"{get_base_url()}/transactions/{transaction_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            return response.json().get("data")
        logger.warning(f"Paddle API returned HTTP {response.status_code}: {response.text}")
    except Exception as exc:
        logger.error(f"Failed to fetch transaction from Paddle: {exc}")
    return None
