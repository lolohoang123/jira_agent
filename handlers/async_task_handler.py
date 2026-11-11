import logging
from jira.tasks.run_today_worklogs_task import RunTodayWorklogsTask
from jira.tasks.run_list_assigned_task import RunListAssignedTask
from jira.tasks.run_search_issues_task import RunSearchIssuesTask
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


def _notify_user_directly(user_id, message):
    """Best-effort DM from the bot: the original channel may be the reason delivery failed."""
    from slack.slack_client import slack_api
    try:
        slack_api("chat.postMessage", {"channel": user_id, "text": message})
    except Exception:
        logger.exception("Could not notify Slack user %s directly", user_id)


def handle_async_task(event):
    # Async ("Event") invocations are retried by Lambda when the handler raises. The tasks below are
    # not idempotent (a retry would create a second approval or repeat the agent reply), so failures
    # are logged and returned instead of raised.
    task=event.get("_async_task")
    if task == "request_worklog_approval":
        from approval.approval_service import (ApprovalService, ApprovalDeliveryError, DuplicateApprovalError,
                                               duplicate_approval_message, undelivered_approval_message)
        user_id = event["slack_user_id"]
        try:
            ApprovalService().request_worklog_approval(user_id, event["channel_id"], event["form"])
        except DuplicateApprovalError:
            _notify_user_directly(user_id, duplicate_approval_message(event["form"]))
            return json_response(409, {"error": "duplicate_approval"})
        except ApprovalDeliveryError:
            _notify_user_directly(user_id, undelivered_approval_message(event["form"], event["channel_id"]))
            return json_response(502, {"error": "approval_not_delivered"})
        except Exception:
            logger.exception("Could not create worklog approval for Slack user %s", user_id)
            _notify_user_directly(user_id, "⚠️ Your worklog request could not be created. Nothing was logged to Jira. "
                                           "Please try again.")
            return json_response(500, {"error": "approval_request_failed"})
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
        from util.config import AGENT_DISABLED_MESSAGE, agent_enabled
        if not agent_enabled():
            # Requests queued before the flag was turned off: answer without calling Bedrock.
            try:
                post_slack_response(event["response_url"], {"response_type": "ephemeral",
                                                            "text": AGENT_DISABLED_MESSAGE})
            except Exception:
                logger.exception("Could not send agent-disabled notice to Slack user %s", event.get("slack_user_id"))
            return json_response(200, {"ok": False, "error": "agent_disabled"})
        try:
            result = AgentOrchestrator().handle(
                slack_user_id=event["slack_user_id"],
                text=event["text"],
                channel_id=event["channel_id"],
            )
            post_slack_response(event["response_url"], {
                "response_type": "ephemeral",
                "text": result.message,
            })
        except Exception:
            logger.exception("Agent request failed for Slack user %s", event.get("slack_user_id"))
            return json_response(500, {"error": "agent_request_failed"})
    elif task=="jira_worklog":
        RunTodayWorklogsTask(event).run_today_worklogs_task()
    elif task=="list_assigned_issues":
        RunListAssignedTask(event).run_list_assigned_task()
    elif task=="search_issues":
        RunSearchIssuesTask(event).run()
    else:
        return json_response(400,{"error":"unknown task"})
    return json_response(200,{"ok":True})
