"""Slack interactivity: button clicks (block_actions) and the Log Time modal submit (view_submission).

Both must answer Slack within 3 seconds, so anything that touches Jira is handed to an async task.
"""
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from slack.installation_store import SlackNotInstalled
from slack.slack_client import slack_api
from slack.modal_builder import build_log_time_modal, get_modal_form
from util.common import json_response, validate_duration
from util.config import get_secret
from util.timezone import user_timezone_name

logger=logging.getLogger(__name__)


def team_id_of(payload):
    """Workspace of an interactivity payload (team.id; user.team_id as a fallback)."""
    return (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id") or ""


def handle_block_actions(payload, context=None, lambda_client=None):
    """Handle a button click: Approve/Reject on an approval message, or Log Time on a ticket row."""
    actions = payload.get("actions", [])
    team_id = team_id_of(payload)
    if not team_id:
        logger.warning("Ignoring block action without a workspace id")
        return json_response(200, "")
    # Approve / Reject: forward to the async task. The clicking user comes from the signed payload; the
    # approval service later checks that this user owns the approval, so nobody else can approve it.
    action = next((a for a in actions if a.get("action_id") in {"approve_jira_write", "reject_jira_write"}), None)
    if action:
        task = {"_async_task": "handle_approval_action", "_task_secret": get_secret("ASYNC_TASK_SECRET"),
                "approval_id": action.get("value"), "decision": action.get("action_id"),
                "team_id": team_id,
                "slack_user_id": payload.get("user", {}).get("id"),
                "channel_id": payload.get("channel", {}).get("id"),
                "message_ts": payload.get("message", {}).get("ts")}
        lambda_client.invoke(FunctionName=context.function_name, InvocationType="Event", Payload=json.dumps(task).encode())
        return json_response(200, "")
    # Log Time: open the modal right away (trigger_id is only valid for 3 seconds), with the ticket key
    # pre-filled from the button value. Any other action id is ignored.
    action=next((a for a in actions if a.get("action_id")=="open_log_time"),None)
    if not action:
        return json_response(200,"")
    user=payload["user"]["id"]
    channel=payload.get("channel",{}).get("id","")
    # Default date = "today" in the user's own timezone (one DynamoDB read, no Jira call).
    tz=ZoneInfo(user_timezone_name(team_id,user))
    today=datetime.now(tz).date().isoformat()
    try:
        slack_api("views.open",{"trigger_id":payload["trigger_id"],"view":build_log_time_modal(user,channel,today,action.get("value",""))},team_id)
    except SlackNotInstalled:
        logger.warning("Log Time clicked in workspace %s without an installation", team_id)
    return json_response(200,"")


def handle_view_submission(payload, context, lambda_client):
    """Validate the Log Time modal and queue an approval request; Jira is NOT written here."""
    view=payload["view"]
    form=get_modal_form(view)
    metadata=json.loads(view.get("private_metadata") or "{}")
    # Field-level validation: errors are shown next to the fields and the modal stays open.
    errors={}
    if not form["issue_key"]:
        errors["issue_block"]="Issue is required."
    elif not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+",form["issue_key"]):
        errors["issue_block"]="Enter a valid issue key, e.g. PROJ-123."
    if not form["date"]: errors["date_block"]="Date is required."
    if not form["start_time"]: errors["start_time_block"]="Start Time is required."
    try: validate_duration(form["duration"])
    except ValueError as exc: errors["duration_block"]=str(exc)
    if form["remaining_estimate"] not in {"leave","auto","new"}:
        errors["estimate_block"]="Choose a valid estimate option."
    if form["remaining_estimate"]=="new" and not form["new_estimate"]:
        errors["new_estimate_block"]="Enter a new remaining estimate."
    if errors:
        return json_response(200,{"response_action":"errors","errors":errors})
    # The submitter (from the signed payload) owns the approval, in the submitter's workspace.
    owner=(payload.get("user") or {}).get("id") or metadata["user_id"]
    team_id=team_id_of(payload)
    if not team_id:
        return json_response(200,{"response_action":"errors","errors":{"issue_block":"Could not identify your Slack workspace. Please try again."}})
    # Creating the approval and sending the DM happen asynchronously; the modal closes immediately.
    task={"_async_task":"request_worklog_approval","_task_secret":get_secret("ASYNC_TASK_SECRET"),
          "form":form,"user_id":owner,"channel_id":metadata["channel_id"],
          "team_id":team_id,"slack_user_id":owner}
    lambda_client.invoke(FunctionName=context.function_name,InvocationType="Event",Payload=json.dumps(task).encode())
    return json_response(200,{"response_action":"clear"})
