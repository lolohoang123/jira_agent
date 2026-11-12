import logging
from jira.jira_service import JiraService
from slack.slack_client import post_slack_response
from slack.message_builder import build_today_worklog_blocks

logger=logging.getLogger(__name__)


class RunTodayWorklogsTask:
    def __init__(self,event): self.event=event

    def run(self):
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

    def run_today_worklogs_task(self): self.run()
