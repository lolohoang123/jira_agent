"""Lambda entry point: one function serves every HTTP route and every async self-invocation.

Routing order matters:
1. GET /slack/install, GET /slack/oauth/callback  - "Add to Slack" install flow (browser, no Slack signature).
2. GET /oauth/callback, GET /oauth/select-site    - Jira connection flow (browser, no Slack signature).
3. Async self-invocations (``_async_task`` set)   - authenticated with the shared async task secret, not by Slack.
4. Everything else is a Slack request             - the Slack signature is verified first, then
   /slack/events goes to the Events API handler and the rest to the slash command / interactivity handler.
"""
import json
import hmac
import logging
import urllib.parse

import boto3

from handlers.oauth_handler import handle_oauth_callback, handle_site_selection
from handlers.slack_command_handler import handle_slack_request
from handlers.async_task_handler import handle_async_task
from handlers.slack_events_handler import handle_slack_event
from handlers.slack_install_handler import handle_slack_install, handle_slack_oauth_callback
from slack.slack_security import get_raw_body, verify_slack_signature
from util.common import json_response
from util.config import get_secret

logger=logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event,context):
    """Route an API Gateway event (HTTP API v2 or REST v1 shape) or an async task event."""
    # HTTP API (v2) uses rawPath / requestContext.http.method; REST API (v1) uses path / httpMethod.
    path=event.get("rawPath") or event.get("path") or "/"
    method=(event.get("requestContext",{}).get("http",{}).get("method") or event.get("httpMethod") or "").upper()
    normalized_path = path.rstrip("/")
    # Slack OAuth v2 install flow (unsigned browser requests, protected by a one-time state).
    # Checked before the Atlassian callback because "/slack/oauth/callback" also ends with "/oauth/callback".
    if method=="GET" and normalized_path.endswith("/slack/install"):
        return handle_slack_install(event)
    if method=="GET" and normalized_path.endswith("/slack/oauth/callback"):
        return handle_slack_oauth_callback(event)
    # Atlassian redirects the user's browser here after the Jira consent screen.
    if normalized_path.endswith("/oauth/callback") and method=="GET":
        return handle_oauth_callback(event)
    # The user chose one of several Jira sites on the page shown by the callback (one-time state in the link).
    if normalized_path.endswith("/oauth/select-site") and method=="GET":
        return handle_site_selection(event)
    # Async task sent by this same function (util/lambda_invoke.py) to do slow work after Slack got its ACK.
    # It does not come through API Gateway, so it is authenticated with the shared task secret instead of
    # a Slack signature. A missing secret rejects every task (fail closed); the comparison is constant-time.
    if event.get("_async_task"):
        expected_secret=get_secret("ASYNC_TASK_SECRET", default="")
        provided_secret=event.get("_task_secret")
        if (not expected_secret or not isinstance(provided_secret,str)
                or not hmac.compare_digest(provided_secret.encode("utf-8"),expected_secret.encode("utf-8"))):
            return json_response(403,{"error":"forbidden"})
        return handle_async_task(event)
    # Every remaining route is a Slack request: reject it unless the signature over the raw body is valid.
    try:
        raw_body=get_raw_body(event)
        if not verify_slack_signature(event,raw_body):
            return json_response(401,{"error":"invalid Slack signature"})
        # Events API (app_uninstalled, tokens_revoked, url_verification).
        if normalized_path.endswith("/slack/events"):
            return handle_slack_event(event,context,raw_body,boto3.client("lambda"))
        # Slash command (/jira-agent) or interactivity payload (button click, modal submit).
        return handle_slack_request(event,context,raw_body,boto3.client("lambda"))
    except Exception:
        # Never leak details to Slack; the stack trace goes to CloudWatch only (no request body is logged).
        logger.exception("Unhandled Slack request error")
        return json_response(500,{"error":"internal_server_error"})
