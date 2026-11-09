import logging
import urllib.error

from approval.approval_repository import ApprovalRepository, DuplicateApprovalError  # noqa: F401 (re-export)
from jira.jira_service import JiraService
from slack.slack_client import slack_api
from util.common import slack_escape

logger = logging.getLogger(__name__)


class ApprovalDeliveryError(Exception):
    """The approval message could not be posted to Slack; the approval was cancelled."""


def _worklog_summary(form):
    # Never include the description: it may be sensitive and is not needed to identify the request.
    return (f"{slack_escape(form.get('issue_key'))} on {slack_escape(form.get('date'))} at "
            f"{slack_escape(form.get('start_time'))} for {slack_escape(form.get('duration'))}")


def duplicate_approval_message(form):
    return (f"⚠️ An identical worklog request ({_worklog_summary(form)}) is already waiting for approval "
            "or was approved in the last 24 hours, so no new request was created.")


def undelivered_approval_message(form, channel_id):
    return (f"⚠️ I couldn't post the approval for your worklog ({_worklog_summary(form)}) in "
            f"<#{channel_id}>. Nothing was logged to Jira. Invite the bot to that channel, or use "
            "`/jira-agent` in a channel where it is a member, then try again.")


def _delivery_unconfirmed(exc):
    """True for timeouts/network errors, where Slack may still have posted the message.

    An HTTP error response (``HTTPError``, a ``URLError`` subclass) or ``ok: false`` (``RuntimeError``
    from ``slack_api``) means Slack definitely did not post it.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, TimeoutError))


class ApprovalService:
    def __init__(self, repository=None):
        self.repository = repository or ApprovalRepository()

    def request_worklog_approval(self, slack_user_id, channel_id, form):
        """Create a PENDING approval and post its Approve/Reject message.

        Raises DuplicateApprovalError for an identical pending/recently approved worklog, and
        ApprovalDeliveryError (after cancelling the approval) when Slack rejects the message.
        """
        item = self.repository.create_pending({
            "slack_user_id": slack_user_id,
            "channel_id": channel_id,
            "action": "create_worklog",
            "form": form,
        })
        try:
            self._post_approval_message(item, channel_id, form)
        except Exception as exc:
            if _delivery_unconfirmed(exc):
                # Slack may have posted it anyway: keep the approval PENDING (it expires in 24 h) rather than
                # leaving visible buttons that refuse every click.
                logger.warning("Delivery of approval %s is unconfirmed (Slack timeout); keeping it pending",
                               item["approval_id"])
                return item
            logger.warning("Could not post approval %s to Slack; cancelling it", item["approval_id"])
            try:
                self.repository.cancel(item["approval_id"], reason=f"Slack delivery failed: {exc}")
            except Exception:
                logger.exception("Could not cancel undelivered approval %s", item["approval_id"])
            raise ApprovalDeliveryError("The approval request could not be posted to Slack.") from exc
        return item

    @staticmethod
    def _post_approval_message(item, channel_id, form):
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

    def approve_and_execute(self, approval_id, slack_user_id):
        item = self.repository.claim(approval_id, slack_user_id)
        if not item:
            return {"ok": False, "message": "This approval is expired, already handled, or does not belong to you."}
        jira_written = False
        try:
            if item["action"] != "create_worklog":
                raise ValueError("Unsupported approved action")
            result = JiraService(slack_user_id).create_worklog(item["form"])
            jira_written = True
            self.repository.finish(approval_id, "APPROVED_EXECUTED")
            return {"ok": True, "result": result, "item": item}
        except Exception as exc:
            try:
                # If Jira already has the worklog, keep the dedupe row so the same worklog cannot be
                # submitted again within 24 h (TC-29).
                self.repository.finish(approval_id, "EXECUTION_FAILED", error=exc, release=not jira_written)
            except Exception:
                # Preserve the original Jira exception instead of masking it.
                logger.exception("Could not mark approval %s as EXECUTION_FAILED", approval_id)
            raise

    def reject(self, approval_id, slack_user_id):
        return self.repository.reject(approval_id, slack_user_id)
