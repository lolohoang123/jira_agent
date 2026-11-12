import json
import hmac
import logging
import urllib.parse

import boto3

from handlers.oauth_handler import handle_oauth_callback
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
    path=event.get("rawPath") or event.get("path") or "/"
    method=(event.get("requestContext",{}).get("http",{}).get("method") or event.get("httpMethod") or "").upper()
    normalized_path = path.rstrip("/")
    # Slack OAuth v2 install flow (unsigned browser requests, protected by a one-time state).
    # Checked before the Atlassian callback because "/slack/oauth/callback" also ends with "/oauth/callback".
    if method=="GET" and normalized_path.endswith("/slack/install"):
        return handle_slack_install(event)
    if method=="GET" and normalized_path.endswith("/slack/oauth/callback"):
        return handle_slack_oauth_callback(event)
    #Handle call back after authentication
    if normalized_path.endswith("/oauth/callback") and method=="GET":
        return handle_oauth_callback(event)
    #handle all event task by slash command in slack
    if event.get("_async_task"):
        expected_secret=get_secret("ASYNC_TASK_SECRET", default="")
        provided_secret=event.get("_task_secret")
        if (not expected_secret or not isinstance(provided_secret,str)
                or not hmac.compare_digest(provided_secret.encode("utf-8"),expected_secret.encode("utf-8"))):
            return json_response(403,{"error":"forbidden"})
        return handle_async_task(event)
    try:
        raw_body=get_raw_body(event)
        if not verify_slack_signature(event,raw_body):
            return json_response(401,{"error":"invalid Slack signature"})
        if normalized_path.endswith("/slack/events"):
            return handle_slack_event(event,context,raw_body,boto3.client("lambda"))
        return handle_slack_request(event,context,raw_body,boto3.client("lambda"))
    except Exception:
        logger.exception("Unhandled Slack request error")
        return json_response(500,{"error":"internal_server_error"})
