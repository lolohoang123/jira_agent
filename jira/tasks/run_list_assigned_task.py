import logging
from jira.jira_service import JiraService
from slack.slack_client import post_slack_response
from slack.message_builder import build_assigned_issue_blocks

logger=logging.getLogger(__name__)


class RunListAssignedTask:
    def __init__(self,event): self.event=event

    def run(self):
        url=self.event["response_url"]
        try:
            service=JiraService(self.event["slack_user_id"])
            issues=service.find_assigned_issues(self.event["account_id"])
            post_slack_response(url,{"response_type":"ephemeral","replace_original":True,
                "text":"Your assigned Jira tickets",
                "blocks":build_assigned_issue_blocks(issues,service.site_url)})
        except Exception:
            logger.exception("Failed to list assigned Jira issues")
            post_slack_response(url,{"response_type":"ephemeral","replace_original":True,
                "text":"Could not list assigned Jira tickets. Check configuration and Lambda logs."})

    def run_list_assigned_task(self): self.run()
