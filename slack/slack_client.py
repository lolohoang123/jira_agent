"""Slack HTTP calls (standard library only): Web API with a workspace bot token, OAuth install exchange,
and replies to a slash command's ``response_url``."""
import json
import logging
import os
import urllib.parse
import urllib.request

from slack.installation_store import InstallationStore
from util.config import get_secret

OAUTH_ACCESS_URL = "https://slack.com/api/oauth.v2.access"

logger = logging.getLogger(__name__)


def slack_api(method, payload, team_id, store=None):
    """Call the Slack Web API with the bot token of workspace ``team_id`` (raises SlackNotInstalled)."""
    token = (store or InstallationStore()).get_bot_token(team_id)
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as response:
        result = json.loads(response.read().decode())
    # Slack returns HTTP 200 with "ok": false on API errors (e.g. channel_not_found).
    if not result.get("ok"):
        raise RuntimeError(f"Slack API {method} failed: {result.get('error', 'unknown_error')}")
    return result


def slack_user_timezone(team_id, slack_user_id, store=None):
    """The Slack profile timezone (``tz``, IANA name) of a user via ``users.info``; None when unavailable.

    Needs the ``users:read`` bot scope: workspaces installed before it was added return ``missing_scope`` until
    they reinstall, which is treated as "unknown" (the caller falls back to the default timezone).
    """
    try:
        token = (store or InstallationStore()).get_bot_token(team_id)
        # Form-encoded: read methods such as users.info do not reliably accept JSON arguments.
        req = urllib.request.Request(
            "https://slack.com/api/users.info", data=urllib.parse.urlencode({"user": slack_user_id}).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())
    except Exception:
        logger.warning("Could not read the Slack timezone of user %s", slack_user_id)
        return None
    if not result.get("ok"):
        logger.warning("Slack users.info failed for user %s: %s", slack_user_id, result.get("error", "unknown_error"))
        return None
    return (result.get("user") or {}).get("tz")


def exchange_install_code(code, redirect_uri):
    """Slack OAuth v2: exchange the install ``code`` for the workspace's bot token (oauth.v2.access)."""
    body = urllib.parse.urlencode({
        "client_id": get_slack_client_id(),
        "client_secret": get_secret("SLACK_CLIENT_SECRET"),
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(OAUTH_ACCESS_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=8) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError(f"Slack oauth.v2.access failed: {result.get('error', 'unknown_error')}")
    return result


def get_slack_client_id():
    """Slack app client id (public value, set by the stack); the client secret comes from SSM."""
    client_id = os.environ.get("SLACK_CLIENT_ID")
    if not client_id:
        raise RuntimeError("Missing required environment variable: SLACK_CLIENT_ID")
    return client_id


def post_slack_response(response_url, message):
    """POST a message to a slash command's response_url (no bot token needed; valid for 30 minutes).

    The host is checked so a forged event can never make us POST to another server.
    """
    parsed = urllib.parse.urlparse(response_url)
    if parsed.scheme != "https" or parsed.hostname not in {"hooks.slack.com", "hooks.slack-gov.com"}:
        raise ValueError("Invalid Slack response_url host")
    req = urllib.request.Request(response_url, data=json.dumps(message).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as response:
        response.read()
