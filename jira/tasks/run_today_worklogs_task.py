"""Async task for /jira-agent logwork today: today's worklogs of the user, replied via response_url."""
import logging
from jira.jira_service import JiraService
from slack.slack_client import post_slack_response
from slack.message_builder import build_today_worklog_blocks

logger=logging.getLogger(__name__)


class RunTodayWorklogsTask:
    """``event`` holds team_id, slack_user_id, account_id and the command's response_url."""
    def __init__(self,event): self.event=event

    def run(self):
        """Replace the "Fetching..." ACK with today's worklogs and total, or with an error message."""
        url=self.event["response_url"]
        try:
            service=JiraService(self.event["team_id"],self.event["slack_user_id"])
            day,entries=service.fetch_today_worklogs(self.event["account_id"])
            post_slack_response(url,{"response_type":"ephemeral","replace_original":True,
                "text":f"Jira Worklogs — {day:%d/%m/%Y}",
                "blocks":build_today_worklog_blocks(day,entries,service.site_url)})
        except Exception:
            logger.exception("Failed to fetch today's Jira worklogs")
            post_slack_response(url,{"response_type":"ephemeral","replace_original":True,
                "text":"Could not fetch Jira worklogs. Check configuration and Lambda logs."})

    def run_today_worklogs_task(self): self.run()  # name used by the async task router
