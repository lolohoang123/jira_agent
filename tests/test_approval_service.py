import unittest
from unittest.mock import patch
from approval.approval_service import ApprovalService

class FakeRepo:
    def __init__(self): self.item=None; self.claimed=False
    def create_pending(self, item): self.item={"approval_id":"abc", **item}; return self.item
    def claim(self, approval_id, user):
        if self.claimed or user != "U1": return None
        self.claimed=True
        return {"approval_id":approval_id,"action":"create_worklog","form":{"issue_key":"PROJ-1"}}
    def finish(self, *args, **kwargs): pass
    def reject(self, *args): return {"status":"REJECTED"}

class ApprovalServiceTests(unittest.TestCase):
    @patch("approval.approval_service.slack_api")
    def test_request_creates_pending_and_posts_buttons(self, slack_api):
        repo=FakeRepo(); service=ApprovalService(repo)
        item=service.request_worklog_approval("U1","C1",{"issue_key":"PROJ-1","date":"2026-11-05","start_time":"09:00","duration":"1h","description":"test"})
        self.assertEqual(item["approval_id"],"abc")
        self.assertEqual(slack_api.call_args.args[0],"chat.postMessage")
    @patch("approval.approval_service.JiraService")
    def test_approve_executes_once(self, jira_service):
        repo=FakeRepo(); service=ApprovalService(repo)
        first=service.approve_and_execute("abc","U1")
        second=service.approve_and_execute("abc","U1")
        self.assertTrue(first["ok"]); self.assertFalse(second["ok"])
        jira_service.assert_called_once_with("U1")

if __name__ == "__main__": unittest.main()
