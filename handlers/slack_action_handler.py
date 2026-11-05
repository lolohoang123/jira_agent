import json
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from slack.slack_client import slack_api
from slack.modal_builder import build_log_time_modal, get_modal_form
from util.common import json_response, validate_duration

logger=logging.getLogger(__name__)


def handle_block_actions(payload, context=None, lambda_client=None):
    actions = payload.get("actions", [])
    action = next((a for a in actions if a.get("action_id") in {"approve_jira_write", "reject_jira_write"}), None)
    if action:
        task = {"_async_task": "handle_approval_action", "_task_secret": os.environ["ASYNC_TASK_SECRET"],
                "approval_id": action.get("value"), "decision": action.get("action_id"),
                "slack_user_id": payload.get("user", {}).get("id"),
                "channel_id": payload.get("channel", {}).get("id"),
                "message_ts": payload.get("message", {}).get("ts")}
        lambda_client.invoke(FunctionName=context.function_name, InvocationType="Event", Payload=json.dumps(task).encode())
        return json_response(200, "")
    action=next((a for a in actions if a.get("action_id")=="open_log_time"),None)
    if not action:
        return json_response(200,"")
    user=payload["user"]["id"]
    channel=payload.get("channel",{}).get("id","")
    tz=ZoneInfo(os.environ.get("JIRA_TIMEZONE","Asia/Ho_Chi_Minh"))
    today=datetime.now(tz).date().isoformat()
    slack_api("views.open",{"trigger_id":payload["trigger_id"],"view":build_log_time_modal(user,channel,today,action.get("value",""))})
    return json_response(200,"")


def handle_view_submission(payload, context, lambda_client):
    view=payload["view"]
    form=get_modal_form(view)
    metadata=json.loads(view.get("private_metadata") or "{}")
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
    task={"_async_task":"request_worklog_approval","_task_secret":os.environ["ASYNC_TASK_SECRET"],
          "form":form,"user_id":metadata["user_id"],"channel_id":metadata["channel_id"],
          "slack_user_id":metadata["user_id"]}
    lambda_client.invoke(FunctionName=context.function_name,InvocationType="Event",Payload=json.dumps(task).encode())
    return json_response(200,{"response_action":"clear"})
