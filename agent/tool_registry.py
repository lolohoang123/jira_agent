"""Allow-listed Jira tools. No model-provided function names are executed dynamically."""
import re
from datetime import datetime

from jira.jira_service import JiraService
from util.dynamo_helper import DynamoHelper
from util.common import validate_duration
from approval.approval_service import (ApprovalService, ApprovalDeliveryError, DuplicateApprovalError,
                                       duplicate_approval_message, undelivered_approval_message)

ALLOWED_INTENTS = {
    "list_assigned_issues", "get_today_worklogs", "get_issue_worklogs",
    "prepare_worklog", "clarify", "unknown",
}


def _validate_issue_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", value.strip()):
        raise ValueError("Issue key is invalid. Example: PROJ-123.")
    return value.strip().upper()


def _validate_worklog_form(parameters):
    if not isinstance(parameters, dict):
        raise ValueError("Worklog parameters must be an object.")
    form = {
        "issue_key": _validate_issue_key(parameters.get("issue_key")),
        "date": parameters.get("date"),
        "duration": validate_duration(parameters.get("duration", "")),
        "start_time": parameters.get("start_time"),
        "description": str(parameters.get("description") or "").strip(),
        "remaining_estimate": parameters.get("remaining_estimate", "leave"),
    }
    try:
        datetime.strptime(form["date"], "%Y-%m-%d")
        datetime.strptime(form["start_time"], "%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError("Worklog date must be YYYY-MM-DD and start_time must be HH:MM.") from exc
    if form["remaining_estimate"] not in {"leave", "auto", "new"}:
        raise ValueError("Invalid remaining_estimate. Use leave, auto, or new.")
    if form["remaining_estimate"] == "new":
        new_estimate = parameters.get("new_estimate")
        if not isinstance(new_estimate, str) or not new_estimate.strip():
            raise ValueError("new_estimate is required when remaining_estimate is new.")
        form["new_estimate"] = new_estimate.strip()
    return form


def execute_plan(plan, team_id, slack_user_id, channel_id):
    """Validate the model plan and execute exactly one allow-listed operation."""
    if not isinstance(plan, dict):
        raise ValueError("Agent plan must be an object.")
    intent = plan.get("intent")
    if intent not in ALLOWED_INTENTS:
        raise ValueError("The requested Jira action is not enabled.")
    parameters = plan.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError("Agent parameters must be an object.")

    if intent == "clarify":
        return {"intent": intent, "message": str(plan.get("message") or "Please provide more details.")[:1500]}
    if intent == "unknown":
        return {"intent": intent, "message": str(plan.get("message") or "I couldn't understand that Jira request.")[:1500]}

    if intent == "prepare_worklog":
        try:
            form = _validate_worklog_form(parameters)
        except (TypeError, ValueError) as exc:
            return {
                "intent": "clarify",
                "message": f"I need a little more information before preparing the worklog: {exc}",
            }
        # Ensure this Slack user has linked a Jira account before creating an approval.
        DynamoHelper("OAUTH_USERS_TABLE").get_jira_account_id_for_slack_user(team_id, slack_user_id)
        try:
            approval = ApprovalService().request_worklog_approval(team_id, slack_user_id, channel_id, form)
        except DuplicateApprovalError:
            return {"intent": "unknown", "message": duplicate_approval_message(form)}
        except ApprovalDeliveryError:
            return {"intent": "unknown", "message": undelivered_approval_message(form, channel_id)}
        return {"intent": intent, "approval_id": approval["approval_id"]}

    # Resolve the Jira account mapping before any read operation.
    account_id = DynamoHelper("OAUTH_USERS_TABLE").get_jira_account_id_for_slack_user(team_id, slack_user_id)
    service = JiraService(team_id, slack_user_id)

    if intent == "list_assigned_issues":
        return {"intent": intent, "data": service.find_assigned_issues(account_id)}
    if intent == "get_today_worklogs":
        day, logs = service.fetch_today_worklogs(account_id)
        return {"intent": intent, "day": day.strftime("%Y-%m-%d"), "data": logs}
    if intent == "get_issue_worklogs":
        issue_key = _validate_issue_key(parameters.get("issue_key"))
        return {"intent": intent, "issue_key": issue_key, "data": service.get_issue_worklogs(issue_key)}
    raise ValueError("Unsupported Jira intent")
