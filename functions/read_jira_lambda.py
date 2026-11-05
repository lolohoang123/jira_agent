import json
import logging
import os
import urllib.parse

import boto3

from handlers.oauth_handler import handle_oauth_callback
from handlers.slack_command_handler import handle_slack_request
from handlers.async_task_handler import handle_async_task
from slack.slack_security import get_raw_body, verify_slack_signature
from util.common import json_response

logger=logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event,context):
    path=event.get("rawPath") or event.get("path") or "/"
    method=(event.get("requestContext",{}).get("http",{}).get("method") or event.get("httpMethod") or "").upper()
    if path.rstrip("/")=="/oauth/callback" and method=="GET":
        return handle_oauth_callback(event)
    if event.get("_async_task"):
        if not os.environ.get("ASYNC_TASK_SECRET") or event.get("_task_secret")!=os.environ.get("ASYNC_TASK_SECRET"):
            return json_response(403,{"error":"forbidden"})
        return handle_async_task(event)
    try:
        raw_body=get_raw_body(event)
        if not verify_slack_signature(event,raw_body):
            return json_response(401,{"error":"invalid Slack signature"})
        return handle_slack_request(event,context,raw_body,boto3.client("lambda"))
    except Exception:
        logger.exception("Unhandled Slack request error")
        return json_response(500,{"error":"internal_server_error"})
