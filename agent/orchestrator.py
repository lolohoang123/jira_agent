"""LLM-assisted intent planning with deterministic, allow-listed execution."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.instructions import AGENT_INSTRUCTIONS
from agent.llm_client import BedrockLLMClient
from agent.schemas import AgentResult
from agent.tool_registry import execute_plan
from approval.approval_service import approval_posted_message
from util.timezone import user_timezone_name

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    def __init__(self, llm_client=None, timezone_lookup=None):
        self.llm = llm_client or BedrockLLMClient()
        # (team_id, slack_user_id) -> IANA timezone name; injectable so tests need no DynamoDB.
        self.timezone_lookup = timezone_lookup or user_timezone_name

    def handle(self, slack_user_id: str, text: str, channel_id: str = "", team_id: str = "") -> AgentResult:
        text = (text or "").strip()
        if not text:
            return AgentResult("Tell me what Jira information you need.")
        if not slack_user_id or not channel_id or not team_id:
            return AgentResult("I couldn't identify the Slack user or channel for this request.")
        tz_name = self.timezone_lookup(team_id, slack_user_id)
        now = datetime.now(ZoneInfo(tz_name))
        context = (
            f"Current date: {now.date().isoformat()}\n"
            f"Current local datetime: {now.isoformat()}\n"
            f"Timezone: {tz_name}\n"
            f"User request: {text}"
        )
        try:
            plan = self.llm.generate_plan(AGENT_INSTRUCTIONS, context)
            outcome = execute_plan(plan, team_id, slack_user_id, channel_id)
            return AgentResult(self._format_outcome(outcome))
        except Exception:
            logger.exception("Agent request failed for Slack user %s", slack_user_id)
            return AgentResult("I couldn't complete that Jira request. Check that Jira is connected and review the Lambda logs.")

    @staticmethod
    def _format_outcome(outcome):
        intent = outcome["intent"]
        if intent == "clarify" or intent == "unknown":
            return outcome["message"]
        if intent == "prepare_worklog":
            return approval_posted_message()
        if intent == "list_assigned_issues":
            issues = outcome.get("data", [])
            if not issues:
                return "You don't have any assigned Jira tickets."
            lines = [f"• {i.get('key', '')} — {i.get('fields', {}).get('summary', '')}" for i in issues[:15]]
            return "Your assigned Jira tickets:\n" + "\n".join(lines)
        if intent == "get_today_worklogs":
            logs = outcome.get("data", [])
            if not logs:
                return f"No worklogs found for {outcome.get('day')}."
            lines = [f"• {x['key']} — {x['started']:%H:%M}, {x.get('seconds', 0)//3600}h {(x.get('seconds', 0)%3600)//60}m" for x in logs[:20]]
            return f"Your worklogs for {outcome.get('day')}:\n" + "\n".join(lines)
        if intent == "get_issue_worklogs":
            logs = outcome.get("data", [])
            if not logs:
                return f"No worklogs found for {outcome['issue_key']}."
            lines = [f"• {w.get('author', {}).get('displayName', 'Unknown')}: {w.get('timeSpent', '')} — {w.get('started', '')}" for w in logs[:20]]
            return f"Worklogs for {outcome['issue_key']}:\n" + "\n".join(lines)
        return "The Jira request was processed."
