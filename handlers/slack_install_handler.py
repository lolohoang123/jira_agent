"""Slack OAuth v2 install flow ("Add to Slack") for multi-workspace distribution.

GET /slack/install         -> 302 to slack.com/oauth/v2/authorize with a one-time state (CSRF protection)
GET /slack/oauth/callback  -> consume the state, exchange the code (oauth.v2.access), store the bot token
                              for the workspace in the installations table.

Enterprise Grid org-wide installs are not supported: they are refused with an explanatory page.
"""
import logging
import os
from urllib.parse import urlencode

from jira.authentication_jira import AuthenticationJira
from slack.installation_store import InstallationStore
from slack.slack_client import exchange_install_code, get_slack_client_id
from util.constant import SLACK_AUTHORIZE_URL, SLACK_BOT_SCOPES
from util.dynamo_helper import DynamoHelper

logger = logging.getLogger(__name__)


def _page(status, title, message):
    """Simple HTML result page shown in the installer's browser (same template as the Jira callback)."""
    return AuthenticationJira({}).html_response(status, title, message)


def handle_slack_install(event):
    """Redirect the browser to Slack's consent screen with our scopes and a fresh one-time state."""
    try:
        # The state row (10-minute TTL) is checked and consumed by the callback below.
        state = DynamoHelper("OAUTH_STATES_TABLE").create_slack_install_state()
        params = {"client_id": get_slack_client_id(), "scope": ",".join(SLACK_BOT_SCOPES),
                  "redirect_uri": os.environ["SLACK_REDIRECT_URI"], "state": state}
    except Exception:
        logger.exception("Could not start the Slack install flow")
        return _page(500, "Could not install the app", "The install link could not be created. Try again later.")
    return {"statusCode": 302, "headers": {"Location": f"{SLACK_AUTHORIZE_URL}?{urlencode(params)}",
                                           "Cache-Control": "no-store"}, "body": ""}


def handle_slack_oauth_callback(event):
    """Finish the install: verify the state, exchange the code for a bot token and store it per workspace."""
    params = event.get("queryStringParameters") or {}
    # The installer clicked "Cancel" on Slack's consent screen.
    if params.get("error"):
        return _page(400, "Installation cancelled", "Slack did not authorize the installation.")
    code, state = params.get("code"), params.get("state")
    if not code or not state:
        return _page(400, "Could not install the app", "Missing authorization code or state. Start again.")
    try:
        # Single use and unexpired; raises ValueError (shown to the user) otherwise. Done before the code
        # exchange so a forged or replayed callback never reaches Slack.
        DynamoHelper("OAUTH_STATES_TABLE").consume_oauth_state(state, kind="slack_install")
        result = exchange_install_code(code, os.environ["SLACK_REDIRECT_URI"])
        # Org-wide (Enterprise Grid) installs have no single team_id, which every table here is keyed on.
        if result.get("is_enterprise_install"):
            return _page(400, "Organization-wide install not supported",
                         "Install the app in each Slack workspace instead of for the whole organization.")
        team = result.get("team") or {}
        token = result.get("access_token")
        if not team.get("id") or not token or result.get("token_type") != "bot":
            raise RuntimeError("Slack did not return a workspace bot token")
        # One row per workspace; reinstalling overwrites it (new token and scopes). The store encrypts the token.
        InstallationStore().save({
            "team_id": team["id"],
            "team_name": team.get("name"),
            "app_id": result.get("app_id"),
            "bot_user_id": result.get("bot_user_id"),
            "bot_token": token,
            "scope": result.get("scope"),
            "installed_by_user_id": (result.get("authed_user") or {}).get("id"),
        })
        logger.info("Slack app installed in workspace %s", team["id"])
        return _page(200, "Jira app installed",
                     f"The app is installed in {team.get('name') or 'your workspace'}. In Slack, run "
                     "`/jira-agent connect` to link your Jira account.")
    except ValueError as exc:
        return _page(400, "Could not install the app", str(exc))
    except Exception:
        logger.exception("Failed to complete the Slack install")
        return _page(500, "Could not install the app", "Installation failed. Check the Lambda logs and try again.")
