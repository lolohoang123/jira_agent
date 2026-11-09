# Test Matrix – Slack request path

Covers `slack/slack_security.py`, `functions/read_jira_lambda.py`, `handlers/slack_command_handler.py`,
`handlers/slack_action_handler.py` and `util/lambda_invoke.py`.
Frozen clock: `time.time() == 1_700_000_000`; local date for the modal: 2026-11-09 (Asia/Ho_Chi_Minh).
Lambda, Slack, DynamoDB and Jira are mocked.

## Signature verification (`verify_slack_signature`, `get_raw_body`)

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-01 | Normal | correct v0 signature, current timestamp | True |
| TC-02 | Normal | header names in mixed case | True (headers matched case-insensitively) |
| TC-03 | Boundary | timestamp exactly 300 s old or 300 s ahead | True |
| TC-04 | Boundary | timestamp 301 s old or 301 s ahead | False (replay window) |
| TC-05 | Abnormal | signature computed with another secret | False |
| TC-06 | Abnormal | body changed after signing | False |
| TC-07 | Abnormal | missing timestamp or signature header, or no headers | False |
| TC-08 | Abnormal | non-numeric timestamp | False |
| TC-09 | Exception | SLACK_SIGNING_SECRET unset | KeyError |
| TC-10 | Normal | isBase64Encoded body | decoded raw body |

## Lambda entry (`lambda_handler`)

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-11 | Normal | GET /oauth/callback (also stage prefix and trailing slash) | OAuth handler; no signature check |
| TC-12 | Abnormal | POST /oauth/callback | treated as Slack request; signature required |
| TC-13 | Normal | async event with correct task secret | handle_async_task called |
| TC-14 | Abnormal | async event with wrong, missing or non-string secret | 403 forbidden; task not run |
| TC-15 | Abnormal | ASYNC_TASK_SECRET unset or empty | 403 even with an empty provided secret |
| TC-16 | Abnormal | Slack request with invalid signature | 401; handler not called |
| TC-17 | Normal | Slack request with valid signature | handle_slack_request with raw body and Lambda client |
| TC-18 | Exception | handler raises | 500 internal_server_error |

## Slash commands and routing (`handle_slack_request`)

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-19 | Normal | interactivity payload type block_actions | handle_block_actions |
| TC-20 | Normal | interactivity payload type view_submission | handle_view_submission |
| TC-21 | Boundary | interactivity payload of another type | 200 empty body |
| TC-22 | Abnormal | command other than /jira-agent | 200 ephemeral hint |
| TC-23 | Abnormal | response_url not https Slack hooks host | 400 invalid response_url; nothing invoked |
| TC-24 | Normal | text "connect" (any case) | build_connect_response(user) |
| TC-25 | Normal | "list", "LIST", "  logwork   today " | async task list_assigned_issues / jira_worklog with account id |
| TC-26 | Abnormal | list when Jira not connected | 200 "not connected" message; nothing invoked |
| TC-27 | Normal | free text "Log 1h on PROJ-1" | agent_request task, original casing kept |
| TC-28 | Exception | ASYNC_TASK_SECRET unset on a task route | KeyError (lambda_handler turns it into 500) |

## Buttons and modal (`handle_block_actions`, `handle_view_submission`)

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-29 | Normal | approve_jira_write / reject_jira_write click | handle_approval_action task with approval id, decision, user, channel, message ts |
| TC-30 | Normal | open_log_time click | views.open with Log Time modal, today's date, issue key prefilled |
| TC-31 | Boundary | no actions or unknown action | 200 empty; nothing invoked or opened |
| TC-32 | Normal | valid modal submission | request_worklog_approval task; owner from metadata; response clear |
| TC-33 | Normal | lower-case issue key "proj-12" | accepted as PROJ-12 |
| TC-34 | Abnormal | missing issue / invalid key / missing date / missing start time | errors on the matching block; nothing invoked |
| TC-35 | Abnormal | duration "0m", "25h", "soon" | duration_block error |
| TC-36 | Abnormal | estimate "new" without a new estimate | new_estimate_block error |
| TC-37 | Abnormal | several fields invalid at once | all errors returned together |

## Async invoke helper (`invoke_async`)

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-38 | Normal | context with invoked_function_arn | invoke the qualified ARN, InvocationType Event |
| TC-39 | Boundary | context without invoked_function_arn | invoke function_name |
| TC-40 | Abnormal | AccessDenied on the ARN | warning; retry with function_name |
| TC-41 | Exception | other ClientError, or AccessDenied on function_name | re-raised; no second call |
