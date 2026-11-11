import logging

from jira.jira_service import JiraService
from slack.message_builder import build_search_result_blocks
from slack.slack_client import post_slack_response

logger = logging.getLogger(__name__)


class RunSearchIssuesTask:
    """Async task for /jira-agent logwork find <keyword>: search Jira and reply with Log Time buttons."""

    def __init__(self, event):
        self.event = event

    def run(self):
        url = self.event["response_url"]
        keyword = self.event["keyword"]
        try:
            service = JiraService(self.event["slack_user_id"])
            issues = service.search_issues(keyword)
            post_slack_response(url, {"response_type": "ephemeral", "replace_original": True,
                                      "text": "Jira tickets matching your search",
                                      "blocks": build_search_result_blocks(keyword, issues, service.site_url)})
        except Exception:
            # The keyword is not logged: it is user-provided free text.
            logger.exception("Failed to search Jira issues for Slack user %s", self.event.get("slack_user_id"))
            post_slack_response(url, {"response_type": "ephemeral", "replace_original": True,
                                      "text": "Could not search Jira tickets. Check your Jira connection and try again."})
