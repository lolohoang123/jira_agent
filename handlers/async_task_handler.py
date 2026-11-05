import logging
from jira.tasks.run_today_worklogs_task import RunTodayWorklogsTask
from jira.tasks.run_list_assigned_task import RunListAssignedTask
from util.common import json_response

logger=logging.getLogger(__name__)


def handle_async_task(event):
    task=event.get("_async_task")
    if task == "request_worklog_approval":
        from approval.approval_service import ApprovalService
        ApprovalService().request_worklog_approval(event["slack_user_id"], event["channel_id"], event["form"])
    elif task == "handle_approval_action":
        from approval.approval_service import ApprovalService
        from slack.slack_client import slack_api
        service = ApprovalService()
        approval_id = event["approval_id"]
        user_id = event["slack_user_id"]
        if event["decision"] == "approve_jira_write":
            result = service.approve_and_execute(approval_id, user_id)
            message = "✅ Jira worklog created successfully." if result["ok"] else f"⚠️ {result['message']}"
        else:
            result = service.reject(approval_id, user_id)
            message = "🚫 Approval rejected." if result else "⚠️ This approval is expired, already handled, or does not belong to you."
        slack_api("chat.postEphemeral", {"channel": event["channel_id"], "user": user_id, "text": message})
        if event.get("message_ts"):
            slack_api("chat.update", {"channel": event["channel_id"], "ts": event["message_ts"],
                                       "text": message,
                                       "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message}}]})
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
