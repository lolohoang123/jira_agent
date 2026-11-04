import base64
import hashlib
import hmac
import os
import time


def get_raw_body(event):
    body = event.get("body") or ""
    return base64.b64decode(body).decode("utf-8") if event.get("isBase64Encoded") else body


def verify_slack_signature(event, raw_body):
    secret = os.environ["SLACK_SIGNING_SECRET"]
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    timestamp, signature = headers.get("x-slack-request-timestamp", ""), headers.get("x-slack-signature", "")
    if not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    base = f"v0:{timestamp}:{raw_body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)
