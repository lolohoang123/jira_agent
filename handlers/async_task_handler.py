import logging
from jira.tasks.run_today_worklogs_task import RunTodayWorklogsTask
from jira.tasks.run_list_assigned_task import RunListAssignedTask
from util.common import json_response

logger=logging.getLogger(__name__)


def _notify_approval_result(event, user_id, message, update_original):
    """Best-effort Slack notification: a Slack failure must not fail (and retry) the task."""
    from slack.slack_client import slack_api
    try:
        slack_api("chat.postEphemeral", {"channel": event["channel_id"], "user": user_id, "text": message})
    except Exception:
        logger.exception("Could not send approval result to Slack user %s", user_id)
    if update_original and event.get("message_ts"):
        try:
            slack_api("chat.update", {"channel": event["channel_id"], "ts": event["message_ts"],
                                       "text": message,
                                       "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message}}]})
        except Exception:
            logger.exception("Could not update approval message for Slack user %s", user_id)


def handle_async_task(event):
    task=event.get("_async_task")
    if task == "request_worklog_approval":
        from approval.approval_service import ApprovalService
        ApprovalService().request_worklog_approval(event["slack_user_id"], event["channel_id"], event["form"])
    elif task == "handle_approval_action":
        from approval.approval_service import ApprovalService
        service = ApprovalService()
        approval_id = event["approval_id"]
        user_id = event["slack_user_id"]
        if event["decision"] == "approve_jira_write":
            try:
                result = service.approve_and_execute(approval_id, user_id)
            except Exception:
                logger.exception("Approval %s could not be completed", approval_id)
                result = {"ok": False, "message": "The approval could not be completed automatically. "
                                                  "Check Jira before submitting again to avoid duplicate worklogs."}
            succeeded = bool(result["ok"])
            message = "✅ Jira worklog created successfully." if succeeded else f"⚠️ {result['message']}"
        else:
            succeeded = bool(service.reject(approval_id, user_id))
            message = "🚫 Approval rejected." if succeeded else "⚠️ This approval is expired, already handled, or does not belong to you."
        # Only a successful approve/reject may replace the original message; otherwise anyone
        # clicking the buttons could wipe the owner's Approve/Reject controls.
        _notify_approval_result(event, user_id, message, update_original=succeeded)
    elif task == "agent_request":
        from agent.orchestrator import AgentOrchestrator
        from slack.slack_client import post_slack_response
        result = AgentOrchestrator().handle(
            slack_user_id=event["slack_user_id"],
            text=event["text"],
            channel_id=event["channel_id"],
        )
        post_slack_response(event["response_url"], {
            "response_type": "ephemeral",
            "text": result.message,
        })
    elif task=="jira_worklog":
        RunTodayWorklogsTask(event).run_today_worklogs_task()
    elif task=="list_assigned_issues":
        RunListAssignedTask(event).run_list_assigned_task()
    else:
        return json_response(400,{"error":"unknown task"})
    return json_response(200,{"ok":True})
