"""Persistence interface placeholder for pending write approvals."""
class ApprovalRepository:
    def create_pending(self, approval):
        raise NotImplementedError("Configure an approvals DynamoDB table before enabling write approvals.")
    def get(self, approval_id):
        raise NotImplementedError
    def mark_approved(self, approval_id, slack_user_id):
        raise NotImplementedError
