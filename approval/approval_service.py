"""Approval boundary: Jira write actions must not execute until explicitly approved."""
class ApprovalService:
    def __init__(self, repository):
        self.repository = repository
    def request_worklog_approval(self, slack_user_id, form):
        return self.repository.create_pending({"slack_user_id":slack_user_id,"action":"create_worklog","form":form,"status":"pending"})
