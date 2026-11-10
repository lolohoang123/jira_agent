# Test Matrix – `approval/` (ApprovalRepository + ApprovalService)

Frozen clock: `time.time() == 1_700_000_000`. DynamoDB table and Slack/Jira are mocked, except the
wire-format rows (TC-47, TC-48), which run a real boto3 resource and capture the request before it is sent.

Retention: every status change after creation sets `ttl = now + APPROVAL_RETENTION_DAYS` (default 90 days).
Duplicates: each approval owns a `dedupe#<sha256>` row, written in the same transaction (see TC-32 to TC-39).

Test files: `tests/test_approval_matrix.py` (TC-01 to TC-31), `tests/test_gap_fixes.py` (TC-32 to TC-46, TC-49),
`tests/test_approval_wire_format.py` (TC-47, TC-48).

## ApprovalRepository (`approval/approval_repository.py`)

| ID | Category | Input | Expected | Test |
|----|----------|-------|----------|------|
| TC-01 | Normal | __init__ with injected table | uses injected table; boto3 not called | `test_tc01_init_with_injected_table_skips_boto3` |
| TC-02 | Abnormal | __init__ without table and APPROVALS_TABLE unset | RuntimeError mentioning APPROVALS_TABLE | `test_tc02_init_without_table_name_raises` |
| TC-03 | Normal | __init__ with APPROVALS_TABLE and no endpoint | boto3.resource("dynamodb") with no kwargs; Table(name) | `test_tc03_init_from_env_uses_default_boto3_resource` |
| TC-04 | Normal | __init__ with DYNAMODB_ENDPOINT_URL set | resource kwargs: endpoint_url, default region ap-southeast-1, local creds | `test_tc04_init_with_local_endpoint_passes_local_kwargs` |
| TC-05 | Normal | create_pending with default ttl | one transaction: PENDING approval (`dedupe_key`, expires_at = ttl = now+86400, `attribute_not_exists`) + dedupe row (`target_approval_id`, expires 24 h, condition `attribute_not_exists OR expires_at <= now`); plain Python values; no `put_item` | `test_tc05_create_pending_default_ttl_writes_pending_item` |
| TC-06 | Boundary | create_pending ttl_seconds=0 | expires_at == created_at == ttl == now | `test_tc06_create_pending_zero_ttl_expires_immediately` |
| TC-07 | Abnormal | create_pending missing "form" key | KeyError; no transaction sent | `test_tc07_create_pending_missing_form_raises_without_write` |
| TC-08 | Normal | get existing approval_id | returns Item; ConsistentRead=True | `test_get_returns_item_or_none[TC-08]` |
| TC-09 | Boundary | get unknown approval_id | returns None | `test_get_returns_item_or_none[TC-09]` |
| TC-10 | Normal | claim by owner while PENDING and unexpired | returns Attributes; condition pending AND owner AND expires_at > now; sets EXECUTING, approved_at, ttl = now + retention | `test_owner_transition_from_pending_returns_attributes[TC-10]` |
| TC-11 | Abnormal | claim fails ConditionalCheckFailedException | returns None | `test_conditional_check_failure_returns_none[TC-11]` |
| TC-12 | Exception | claim raises other ClientError | ClientError re-raised | `test_other_client_errors_are_reraised[TC-12]` |
| TC-13 | Normal | reject by owner while PENDING | returns REJECTED Attributes; same owner/pending/expiry condition; rejected_at, ttl = now + retention; dedupe row released (TC-36) | `test_owner_transition_from_pending_returns_attributes[TC-13]` |
| TC-14 | Abnormal | reject fails ConditionalCheckFailedException | returns None; dedupe row kept | `test_conditional_check_failure_returns_none[TC-14]` |
| TC-15 | Exception | reject raises other ClientError | ClientError re-raised | `test_other_client_errors_are_reraised[TC-15]` |
| TC-16 | Normal | finish APPROVED_EXECUTED without error | condition `#s = :executing`; sets status, completed_at, ttl = now + retention; ReturnValues ALL_NEW; no error_message; dedupe row kept | `test_tc16_finish_without_error_only_sets_status` |
| TC-17 | Boundary | finish with error of 499/500/501 chars | error_message length min(len, 500) | `test_tc17_finish_truncates_error_to_500_chars` |
| TC-18 | Boundary | finish with empty-string error | error_message not written | `test_tc18_finish_with_empty_error_skips_error_message` |
| TC-19 | Exception | finish when row is not EXECUTING | ConditionalCheckFailed ClientError propagates | `test_tc19_finish_on_non_executing_row_propagates_client_error` |

## ApprovalService (`approval/approval_service.py`)

| ID | Category | Input | Expected | Test |
|----|----------|-------|----------|------|
| TC-20 | Normal | request_worklog_approval valid form | create_pending(action=create_worklog); chat.postMessage with Approve/Reject buttons valued approval_id | `test_tc20_request_creates_pending_and_posts_buttons` |
| TC-21 | Normal | form fields containing & < > | issue_key, duration, description Slack-escaped in text | `test_tc21_request_escapes_slack_markup` |
| TC-22 | Boundary | description None or "" | "—" placeholder shown | `test_tc22_request_without_description_shows_placeholder` |
| TC-23 | Abnormal | create_pending raises (incl. DuplicateApprovalError) | exception propagates; slack_api not called | `test_tc23_request_does_not_post_when_create_pending_fails` |
| TC-24 | Normal | approve_and_execute by owner | JiraService(user).create_worklog(form); finish APPROVED_EXECUTED; ok True | `test_tc24_approve_executes_worklog_and_marks_executed` |
| TC-25 | Abnormal | approve_and_execute when claim returns None | ok False with message; Jira and finish not called | `test_tc25_approve_unclaimable_returns_not_ok_without_side_effects` |
| TC-26 | Abnormal | claimed item has unsupported action | ValueError; finish EXECUTION_FAILED with release=True; Jira not called | `test_tc26_approve_unsupported_action_fails_without_jira` |
| TC-27 | Exception | Jira create_worklog raises | same exception re-raised; finish EXECUTION_FAILED with error, release=True | `test_tc27_approve_jira_failure_marks_failed_and_reraises` |
| TC-28 | Exception | Jira raises and finish(EXECUTION_FAILED) also raises | original Jira exception raised; failure logged | `test_tc28_approve_keeps_jira_error_when_finish_also_fails` |
| TC-29 | Exception | Jira succeeds but finish(APPROVED_EXECUTED) raises | marked EXECUTION_FAILED with release=False (dedupe row kept) and raised (see note) | `test_tc29_finish_executed_failure_after_jira_success_marks_failed` |
| TC-30 | Normal | reject by owner | delegates to repository.reject; returns its result | `test_reject_delegates_to_repository[TC-30]` |
| TC-31 | Abnormal | reject by non-owner / already handled | returns None | `test_reject_delegates_to_repository[TC-31]` |

## Duplicate protection (GAP-01)

| ID | Category | Input | Expected | Test |
|----|----------|-------|----------|------|
| TC-32 | Normal | same worklog with different case, spacing or description | same dedupe key | `test_gap01_dedupe_key_ignores_formatting_and_description` |
| TC-33 | Normal | different date, start time, duration, issue or user | different dedupe key | `test_gap01_dedupe_key_differs_for_different_worklogs`, `test_gap01_dedupe_key_differs_per_user` |
| TC-34 | Abnormal | transaction cancelled on the dedupe condition (2nd reason ConditionalCheckFailed) | DuplicateApprovalError with the dedupe key | `test_gap01_duplicate_request_raises_duplicate_error`, `test_duplicate_is_detected_from_real_cancellation_response` |
| TC-35 | Exception | transaction cancelled on the approval-id condition | ClientError re-raised (not reported as duplicate) | `test_gap01_other_transaction_failure_is_reraised` |
| TC-36 | Normal | reject succeeds | dedupe row deleted with condition `target_approval_id = :id` | `test_gap01_reject_releases_dedupe_row` |
| TC-37 | Abnormal | reject refused (non-owner) | dedupe row kept | `test_gap01_rejected_by_non_owner_keeps_dedupe_row` |
| TC-38 | Boundary | finish EXECUTION_FAILED / APPROVED_EXECUTED; release=True / False | released only for EXECUTION_FAILED with release=True | `test_gap01_finish_releases_dedupe_only_on_failure`, `test_review2_finish_failed_respects_release_flag` |
| TC-39 | Exception | release fails / item without dedupe_key | failure swallowed (row expires on its own) / no call | `test_gap01_release_failure_is_swallowed`, `test_gap01_release_without_dedupe_key_is_noop` |

## Undelivered approvals (GAP-02)

| ID | Category | Input | Expected | Test |
|----|----------|-------|----------|------|
| TC-40 | Normal | cancel a PENDING approval | CANCELLED, completed_at, ttl = now + retention, error_message; dedupe row released | `test_gap02_cancel_moves_pending_to_cancelled_and_releases` |
| TC-41 | Abnormal | cancel when not PENDING | returns None; dedupe row kept | `test_gap02_cancel_of_non_pending_returns_none` |
| TC-42 | Abnormal | Slack definitely rejects the message (`ok: false`, HTTP 429) | approval cancelled; ApprovalDeliveryError | `test_gap02_post_failure_cancels_and_raises_delivery_error`, `test_review1_definite_slack_failure_cancels` |
| TC-43 | Exception | Slack rejects and cancel also fails | ApprovalDeliveryError still raised | `test_gap02_cancel_failure_still_raises_delivery_error` |
| TC-44 | Boundary | Slack timeout or network error (delivery unconfirmed) | approval stays PENDING; item returned; no cancel | `test_review1_slack_timeout_keeps_approval_pending` |

## Retention and release decisions (GAP-05, TC-29)

| ID | Category | Input | Expected | Test |
|----|----------|-------|----------|------|
| TC-45 | Boundary | APPROVAL_RETENTION_DAYS unset / 365 / 0 / "abc" | ttl = now + 90 / 365 / 1 / 90 days | `test_gap05_finish_extends_ttl_to_retention` |
| TC-46 | Normal | approve: Jira fails / status update fails after Jira succeeded | finish(..., release=True) / finish(..., release=False) | `test_review2_jira_failure_releases_but_post_jira_failure_does_not`, `test_tc29_finish_executed_failure_after_jira_success_marks_failed` |

## DynamoDB wire format (regression: "operand type: M")

| ID | Category | Input | Expected | Test |
|----|----------|-------|----------|------|
| TC-47 | Normal | create_pending through a real boto3 resource | TransactWriteItems with values typed exactly once (`{"S": "PENDING"}`, `{"N": "…"}`), nested form as `M`, None as `NULL`, `:now` as `N` | `test_create_pending_sends_typed_values_once` |
| TC-48 | Normal | claim, reject, cancel, finish, release through a real boto3 resource | every expression value is a single-level `S`/`N` | `test_other_writes_send_typed_values_once` |

## User-facing messages

| ID | Category | Input | Expected | Test |
|----|----------|-------|----------|------|
| TC-49 | Normal | duplicate / undelivered approval messages (modal DM and agent reply) | name issue, date, start time and duration; never the description | `test_gap02_modal_failures_notify_user_by_dm`, `test_gap02_agent_path_returns_message_instead_of_failing` |

> **Note TC-29:** if marking the row `APPROVED_EXECUTED` fails after Jira accepted the worklog, the service
> records `EXECUTION_FAILED` and raises, so the stored state says the write failed even though it happened.
> Re-execution is still blocked (the row is no longer PENDING), and the dedupe row is **kept**
> (`release=False`), so the same worklog cannot be resubmitted for 24 h. The proper fix is a separate
> state for "Jira write done, status unknown" (spec gap GAP-07).
>
> **Note TC-44:** keeping the approval PENDING after a Slack timeout means the user gets no DM if Slack
> really did not post it; the identical worklog is then blocked as a duplicate until the approval expires (24 h).
