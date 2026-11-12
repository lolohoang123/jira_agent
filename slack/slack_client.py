import json
import os
import urllib.parse
import urllib.request

from slack.installation_store import InstallationStore
from util.config import get_secret

OAUTH_ACCESS_URL = "https://slack.com/api/oauth.v2.access"


def slack_api(method, payload, team_id, store=None):
    """Call the Slack Web API with the bot token of workspace ``team_id`` (raises SlackNotInstalled)."""
    token = (store or InstallationStore()).get_bot_token(team_id)
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError(f"Slack API {method} failed: {result.get('error', 'unknown_error')}")
    return result


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
    client_id = os.environ.get("SLACK_CLIENT_ID")
    if not client_id:
        raise RuntimeError("Missing required environment variable: SLACK_CLIENT_ID")
    return client_id


def post_slack_response(response_url, message):
    parsed = urllib.parse.urlparse(response_url)
    if parsed.scheme != "https" or parsed.hostname not in {"hooks.slack.com", "hooks.slack-gov.com"}:
        raise ValueError("Invalid Slack response_url host")
    req = urllib.request.Request(response_url, data=json.dumps(message).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as response:
        response.read()
