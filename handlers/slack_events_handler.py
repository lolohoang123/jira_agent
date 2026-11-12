"""Slack Events API (POST /slack/events, signature verified by the entry point).

- url_verification: echo the challenge (Slack checks the Request URL).
- app_uninstalled: remove everything of that workspace (installation, every Jira connection and token,
  pending approvals). Done in an async task so Slack gets its answer within 3 seconds.
- tokens_revoked with bot tokens: the bot token no longer works, so the installation is removed; users'
  Jira connections are kept (a reinstall restores the app for them).
Deletions are idempotent, so Slack's retries (X-Slack-Retry-Num) are harmless.
"""
import json
import logging
import os

from util.common import json_response
from util.config import get_secret

logger = logging.getLogger(__name__)


def handle_slack_event(event, context, raw_body, lambda_client):
    try:
        body = json.loads(raw_body or "{}")
    except ValueError:
        return json_response(400, {"error": "invalid JSON"})
    if body.get("type") == "url_verification":
        return json_response(200, {"challenge": body.get("challenge", "")})
    if body.get("type") != "event_callback":
        return json_response(200, {"ok": True, "ignored": True})
    expected_app = os.environ.get("SLACK_APP_ID")
    if expected_app and body.get("api_app_id") != expected_app:
        logger.warning("Ignoring event for another Slack app")
        return json_response(200, {"ok": True, "ignored": True})

    team_id = body.get("team_id")
    inner = body.get("event") or {}
    if inner.get("type") == "app_uninstalled":
        reason = "app_uninstalled"
    elif inner.get("type") == "tokens_revoked" and (inner.get("tokens") or {}).get("bot"):
        reason = "bot_token_revoked"
    else:
        return json_response(200, {"ok": True, "ignored": True})
    if not team_id:
        return json_response(200, {"ok": True, "ignored": True})

    task = {"_async_task": "uninstall_workspace", "_task_secret": get_secret("ASYNC_TASK_SECRET"),
            "team_id": team_id, "reason": reason}
    lambda_client.invoke(FunctionName=context.function_name, InvocationType="Event",
                         Payload=json.dumps(task).encode())
    return json_response(200, {"ok": True})


def uninstall_workspace(team_id, reason="app_uninstalled", store=None, users=None, approvals=None):
    """Remove a workspace's data. Every step runs even if an earlier one fails; failures are raised at the end
    (the async task then lands in the failure queue and can be replayed safely)."""
    from approval.approval_repository import ApprovalRepository
    from slack.installation_store import InstallationStore
    from util.dynamo_helper import DynamoHelper

    steps = [("installation", lambda: (store or InstallationStore()).delete(team_id))]
    if reason == "app_uninstalled":
        steps += [
            ("jira_connections", lambda: (users or DynamoHelper("OAUTH_USERS_TABLE")).delete_team_connections(team_id)),
            ("pending_approvals", lambda: (approvals or ApprovalRepository()).cancel_pending_for_team(team_id)),
        ]
    results, failed = {}, []
    for name, step in steps:
        try:
            results[name] = step()
        except Exception:
            logger.exception("Uninstall step %s failed for workspace %s", name, team_id)
            failed.append(name)
    logger.info("Workspace %s cleanup (%s): %s", team_id, reason, results)
    if failed:
        raise RuntimeError(f"Workspace cleanup incomplete: {', '.join(failed)}")
    return results
