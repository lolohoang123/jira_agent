"""Verify that a request really comes from Slack (signing secret, HMAC-SHA256 over the raw body)."""
import base64
import hashlib
import hmac
import time

from util.config import get_secret


def get_raw_body(event):
    """The request body exactly as Slack sent it (API Gateway may deliver it base64-encoded)."""
    body = event.get("body") or ""
    return base64.b64decode(body).decode("utf-8") if event.get("isBase64Encoded") else body


def verify_slack_signature(event, raw_body):
    """True only for a correctly signed request that is at most 5 minutes old."""
    secret = get_secret("SLACK_SIGNING_SECRET")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    timestamp, signature = headers.get("x-slack-request-timestamp", ""), headers.get("x-slack-signature", "")
    if not timestamp or not signature:
        return False
    # Reject old timestamps so a captured request cannot be replayed later.
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    # Slack signs "v0:<timestamp>:<body>"; compare in constant time.
    base = f"v0:{timestamp}:{raw_body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)
