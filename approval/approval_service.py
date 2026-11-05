import logging

from approval.approval_repository import ApprovalRepository
from jira.jira_service import JiraService
from slack.slack_client import slack_api
from util.common import slack_escape

logger = logging.getLogger(__name__)


class ApprovalService:
    def __init__(self, repository=None):
        self.repository = repository or ApprovalRepository()

    def request_worklog_approval(self, slack_user_id, channel_id, form):
        item = self.repository.create_pending({
            "slack_user_id": slack_user_id,
            "channel_id": channel_id,
            "action": "create_worklog",
            "form": form,
        })
        key = slack_escape(form["issue_key"])
        duration = slack_escape(form["duration"])
        description = slack_escape(form.get("description") or "—")
        slack_api("chat.postMessage", {
            "channel": channel_id,
            "text": f"Approval required: log {duration} to {key}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text":
                 f"*Approve Jira worklog?*\nIssue: *{key}*\nDate: {form['date']}\n"
                 f"Start: {form['start_time']}\nDuration: *{duration}*\nDescription: {description}"}},
                {"type": "actions", "block_id": f"approval_{item['approval_id']}", "elements": [
                    {"type": "button", "action_id": "approve_jira_write", "style": "primary",
                     "text": {"type": "plain_text", "text": "Approve"}, "value": item["approval_id"]},
                    {"type": "button", "action_id": "reject_jira_write", "style": "danger",
                     "text": {"type": "plain_text", "text": "Reject"}, "value": item["approval_id"]},
                ]},
            ],
        })
        return item

    def approve_and_execute(self, approval_id, slack_user_id):
        item = self.repository.claim(approval_id, slack_user_id)
        if not item:
            return {"ok": False, "message": "This approval is expired, already handled, or does not belong to you."}
        try:
            if item["action"] != "create_worklog":
                raise ValueError("Unsupported approved action")
            result = JiraService(slack_user_id).create_worklog(item["form"])
            self.repository.finish(approval_id, "APPROVED_EXECUTED")
            return {"ok": True, "result": result, "item": item}
        except Exception as exc:
            try:
                self.repository.finish(approval_id, "EXECUTION_FAILED", error=exc)
            except Exception:
                # Preserve the original Jira exception instead of masking it.
                logger.exception("Could not mark approval %s as EXECUTION_FAILED", approval_id)
            raise

    def reject(self, approval_id, slack_user_id):
        return self.repository.reject(approval_id, slack_user_id)
