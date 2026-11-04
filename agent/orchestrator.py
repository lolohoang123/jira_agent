"""Agent orchestration placeholder.

The current MVP deliberately routes slash commands through deterministic Python
handlers. Add an LLM here only after tool schemas, authorization and approval
persistence are tested. Do not call Jira REST directly from an LLM.
"""
from agent.schemas import AgentResult

class AgentOrchestrator:
    def handle(self, slack_user_id: str, text: str) -> AgentResult:
        return AgentResult(message="Natural-language agent routing is not enabled yet. Use `/jira-agent list` or `/jira-agent logwork today`.")
