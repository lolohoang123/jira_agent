"""Async task for /jira-agent list: fetch the user's assigned issues and reply via response_url."""
import logging
from jira.jira_service import JiraService
from slack.slack_client import post_slack_response
from slack.message_builder import build_assigned_issue_blocks

logger=logging.getLogger(__name__)


class RunListAssignedTask:
    """``event`` holds team_id, slack_user_id, account_id and the command's response_url."""
    def __init__(self,event): self.event=event

    def run(self):
        """Replace the "Fetching..." ACK with the ticket list, or with an error message."""
        url=self.event["response_url"]
        try:
            service=JiraService(self.event["team_id"],self.event["slack_user_id"])
            issues=service.find_assigned_issues(self.event["account_id"])
            post_slack_response(url,{"response_type":"ephemeral","replace_original":True,
                "text":"Your assigned Jira tickets",
                "blocks":build_assigned_issue_blocks(issues,service.site_url)})
        except Exception:
            logger.exception("Failed to list assigned Jira issues")
            post_slack_response(url,{"response_type":"ephemeral","replace_original":True,
                "text":"Could not list assigned Jira tickets. Check configuration and Lambda logs."})

    def run_list_assigned_task(self): self.run()  # name used by the async task router
