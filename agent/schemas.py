"""Small data types passed between the Slack handlers and the agent orchestrator."""
from dataclasses import dataclass
from typing import Any

@dataclass
class AgentRequest:
    """A free-text request from one Slack user."""
    slack_user_id: str
    text: str
    channel_id: str | None = None

@dataclass
class AgentResult:
    """What the agent replies to the user (``message`` is posted to Slack as-is)."""
    message: str
    action: dict[str, Any] | None = None
    requires_approval: bool = False
