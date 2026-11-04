import logging
from jira.jira_service import JiraService
from slack.slack_client import slack_api
from util.common import slack_escape

logger=logging.getLogger(__name__)


class RunCreateWorklogsTask:
    def __init__(self,event,slack_user_id=None):
        self.event=event
        self.slack_user_id=slack_user_id or event.get("slack_user_id")

    def run_create_worklog_task(self):
        user=self.event["user_id"]; channel=self.event["channel_id"]; form=self.event["form"]
        try:
            service=JiraService(self.slack_user_id)
            result=service.create_worklog(form)
            key=form["issue_key"]
            url=f"{service.site_url}/browse/{key}"
            message=f"✅ Worklog added to <{url}|*{slack_escape(key)}*> for *{slack_escape(form['duration'])}*."
            if result.get("id"): message+=f"\nWorklog ID: `{slack_escape(result['id'])}`"
        except Exception:
            logger.exception("Failed to create Jira worklog")
            message="❌ Could not create the Jira worklog. Check issue key, permissions, duration, and Lambda logs."
        slack_api("chat.postEphemeral",{"channel":channel,"user":user,"text":message})
