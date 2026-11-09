# Test Matrix – `approval/` (ApprovalRepository + ApprovalService)

Frozen clock: `time.time() == 1_700_000_000`. DynamoDB table and Slack/Jira are mocked.

## ApprovalRepository (`approval/approval_repository.py`)

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-01 | Normal | __init__ with injected table | uses injected table; boto3 not called |
| TC-02 | Abnormal | __init__ without table and APPROVALS_TABLE unset | RuntimeError mentioning APPROVALS_TABLE |
| TC-03 | Normal | __init__ with APPROVALS_TABLE and no endpoint | boto3.resource("dynamodb") with no kwargs; Table(name) |
| TC-04 | Normal | __init__ with DYNAMODB_ENDPOINT_URL set | resource kwargs: endpoint_url, default region ap-southeast-1, local creds |
| TC-05 | Normal | create_pending with default ttl | PENDING item; expires_at = ttl = now+86400; put_item with attribute_not_exists |
| TC-06 | Boundary | create_pending ttl_seconds=0 | expires_at == created_at == now |
| TC-07 | Abnormal | create_pending missing "form" key | KeyError; put_item not called |
| TC-08 | Normal | get existing approval_id | returns Item; ConsistentRead=True |
| TC-09 | Boundary | get unknown approval_id | returns None |
| TC-10 | Normal | claim by owner while PENDING and unexpired | returns Attributes; condition checks pending, owner, expires_at > now |
| TC-11 | Abnormal | claim fails ConditionalCheckFailedException | returns None |
| TC-12 | Exception | claim raises other ClientError | ClientError re-raised |
| TC-13 | Normal | reject by owner while PENDING | returns REJECTED Attributes; same owner/pending/expiry condition |
| TC-14 | Abnormal | reject fails ConditionalCheckFailedException | returns None |
| TC-15 | Exception | reject raises other ClientError | ClientError re-raised |
| TC-16 | Normal | finish APPROVED_EXECUTED without error | no error_message; condition "#s = :executing" |
| TC-17 | Boundary | finish with error of 499/500/501 chars | error_message length min(len, 500) |
| TC-18 | Boundary | finish with empty-string error | error_message not written |
| TC-19 | Exception | finish when row is not EXECUTING | ConditionalCheckFailed ClientError propagates |

## ApprovalService (`approval/approval_service.py`)

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-20 | Normal | request_worklog_approval valid form | create_pending(action=create_worklog); chat.postMessage with Approve/Reject buttons valued approval_id |
| TC-21 | Normal | form fields containing & < > | issue_key, duration, description Slack-escaped in text |
| TC-22 | Boundary | description None or "" | "—" placeholder shown |
| TC-23 | Abnormal | create_pending raises | exception propagates; slack_api not called |
| TC-24 | Normal | approve_and_execute by owner | JiraService(user).create_worklog(form); finish APPROVED_EXECUTED; ok True |
| TC-25 | Abnormal | approve_and_execute when claim returns None | ok False with message; Jira and finish not called |
| TC-26 | Abnormal | claimed item has unsupported action | ValueError; finish EXECUTION_FAILED; Jira not called |
| TC-27 | Exception | Jira create_worklog raises | same exception re-raised; finish EXECUTION_FAILED with error |
| TC-28 | Exception | Jira raises and finish(EXECUTION_FAILED) also raises | original Jira exception raised; failure logged |
| TC-29 | Exception | Jira succeeds but finish(APPROVED_EXECUTED) raises | current behaviour: marked EXECUTION_FAILED and raised (see note) |
| TC-30 | Normal | reject by owner | delegates to repository.reject; returns its result |
| TC-31 | Abnormal | reject by non-owner / already handled | returns None |

> **Note TC-29:** if marking the row `APPROVED_EXECUTED` fails after Jira accepted the worklog, the
> service records `EXECUTION_FAILED` and raises, so the stored state says the write failed even though
> it happened. Re-execution is still blocked (the row is no longer PENDING). The test pins the
> current behaviour so any change to it is deliberate.
