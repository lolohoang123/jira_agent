import json
import logging
import os
import urllib.parse
from urllib.parse import parse_qs

from handlers.slack_action_handler import handle_block_actions, handle_view_submission
from jira.authentication_jira import AuthenticationJira
from util.common import json_response
from util.config import AGENT_DISABLED_MESSAGE, agent_enabled, get_secret
from util.rate_limit import agent_limit_message, agent_request_allowed
from util.dynamo_helper import DynamoHelper

logger = logging.getLogger(__name__)


def _valid_response_url(response_url):
    parsed = urllib.parse.urlparse(response_url or "")
    return parsed.scheme == "https" and parsed.hostname in {"hooks.slack.com", "hooks.slack-gov.com"}


def _disconnect(slack_user_id):
    """Delete the user's Jira connection and stored tokens (spec gap GAP-04)."""
    try:
        removed = DynamoHelper("OAUTH_USERS_TABLE").disconnect_slack_user(slack_user_id)
    except Exception:
        logger.exception("Could not disconnect Jira for Slack user %s", slack_user_id)
        return json_response(200, {"response_type": "ephemeral",
                                   "text": "Could not disconnect Jira right now. Please try again."})
    text = ("Your Jira account has been disconnected and its stored tokens deleted. "
            "To also revoke access on the Atlassian side, remove the app under your Atlassian account "
            "settings → Connected apps." if removed else "No Jira account is connected.")
    return json_response(200, {"response_type": "ephemeral", "text": text})


def handle_slack_request(event, context, raw_body, lambda_client):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if "application/x-www-form-urlencoded" in headers.get("content-type", ""):
        form = parse_qs(raw_body)
        if "payload" in form:
            payload = json.loads(form["payload"][0])
            if payload.get("type") == "block_actions":
                return handle_block_actions(payload, context, lambda_client)
            if payload.get("type") == "view_submission":
                return handle_view_submission(payload, context, lambda_client)
            return json_response(200, "")

    data = parse_qs(raw_body)
    command = data.get("command", [""])[0]
    text = " ".join(data.get("text", [""])[0].split())  # preserve original casing for the LLM
    normalized = text.lower()
    slack_user_id = data.get("user_id", [""])[0]
    channel_id = data.get("channel_id", [""])[0]
    response_url = data.get("response_url", [""])[0]

    if command != "/jira-agent":
        return json_response(200, {"response_type": "ephemeral", "text": "Use `/jira-agent` to ask Jira."})
    if not _valid_response_url(response_url):
        return json_response(400, {"error": "invalid response_url"})

    if normalized == "connect":
        return AuthenticationJira({}).build_connect_response(slack_user_id)
    if normalized == "disconnect":
        return _disconnect(slack_user_id)

    supported = {"logwork today", "list"}
    if normalized not in supported:
        if not agent_enabled():
            # Checked before the rate limit and the async invoke: no Bedrock call, no counter used.
            return json_response(200, {"response_type": "ephemeral", "text": AGENT_DISABLED_MESSAGE})
        if not agent_request_allowed(slack_user_id):
            return json_response(200, {"response_type": "ephemeral", "text": agent_limit_message()})
        task = {
            "_async_task": "agent_request",
            "_task_secret": get_secret("ASYNC_TASK_SECRET"),
            "slack_user_id": slack_user_id,
            "channel_id": channel_id,
            "text": text,
            "response_url": response_url,
        }
        lambda_client.invoke(
            FunctionName=context.function_name,
            InvocationType="Event",
            Payload=json.dumps(task).encode(),
        )
        return json_response(200, {"response_type": "ephemeral", "text": "I'm processing your Jira request..."})

    try:
        account_id = DynamoHelper("OAUTH_USERS_TABLE").get_jira_account_id_for_slack_user(slack_user_id)
    except Exception:
        return json_response(200, {"response_type": "ephemeral", "text": "Your Jira account is not connected. Run `/jira-agent connect` first."})

    task_type = "jira_worklog" if normalized == "logwork today" else "list_assigned_issues"
    task = {
        "_async_task": task_type,
        "_task_secret": get_secret("ASYNC_TASK_SECRET"),
        "slack_user_id": slack_user_id,
        "account_id": account_id,
        "response_url": response_url,
    }
    lambda_client.invoke(
        FunctionName=context.function_name,
        InvocationType="Event",
        Payload=json.dumps(task).encode(),
    )
    return json_response(200, {"response_type": "ephemeral", "text": "Fetching your Jira data..."})
