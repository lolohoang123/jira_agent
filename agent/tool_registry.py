"""Explicit allow-list of backend capabilities available to a future agent."""
TOOLS = {
    "list_assigned_issues": {"read_only": True},
    "get_today_worklogs": {"read_only": True},
    "create_worklog": {"read_only": False, "requires_approval": True},
}
