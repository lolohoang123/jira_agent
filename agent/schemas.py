from dataclasses import dataclass
from typing import Any

@dataclass
class AgentRequest:
    slack_user_id: str
    text: str

@dataclass
class AgentResult:
    message: str
    action: dict[str, Any] | None = None
    requires_approval: bool = False
