# Test Matrix – Jira write, agent allow-list, approval actions, Slack client

Covers `jira/jira_service.create_worklog`, `agent/tool_registry.py`, the `handle_approval_action` task in
`handlers/async_task_handler.py`, and `slack/slack_client.py`.
Jira HTTP, DynamoDB, Slack and the approval service are mocked. Default `JIRA_TIMEZONE=Asia/Ho_Chi_Minh`.

## Jira worklog write (`JiraService.create_worklog`) – the only Jira write

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-01 | Normal | PROJ-12, 2026-11-09 09:00, 1h 30m, description, leave | POST `/rest/api/3/issue/PROJ-12/worklog`, `started=2026-11-09T09:00:00.000+0700`, `timeSpent=1h 30m`, comment in Atlassian document format, `adjustEstimate=leave` |
| TC-02 | Boundary | JIRA_TIMEZONE UTC / Asia/Kolkata / America/New_York in November and July | offset `+0000` / `+0530` / `-0500` / `-0400` (DST) |
| TC-03 | Boundary | empty or missing description | no `comment` field |
| TC-04 | Normal | description with Vietnamese, emoji, `<&>` and newlines | text sent unchanged inside the document (no Slack escaping) |
| TC-05 | Normal | remaining_estimate auto / new with new_estimate 2h | `adjustEstimate=auto` / `adjustEstimate=new&newEstimate=2h` |
| TC-06 | Abnormal | issue key lower-case, missing number, with spaces | ValueError; no Jira call |
| TC-07 | Abnormal | duration 0m / 25h / text | ValueError; no Jira call |
| TC-08 | Abnormal | estimate "keep" / new without new_estimate | ValueError; no Jira call |
| TC-09 | Abnormal | date 2026-02-30 / time 25:00 | ValueError; no Jira call |
| TC-10 | Normal | Jira response | returned unchanged |

## Agent allow-list (`execute_plan`, `_validate_worklog_form`)

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-11 | Abnormal | plan not an object (list, string, None) | ValueError "must be an object" |
| TC-12 | Abnormal | intent outside ALLOWED_INTENTS (create_worklog, delete_worklog, `__class__`, None) | ValueError "not enabled"; no DynamoDB, Jira or approval call |
| TC-13 | Abnormal | parameters not an object | ValueError |
| TC-14 | Normal | clarify / unknown with message | message returned; no external call |
| TC-15 | Boundary | clarify message missing, non-string, 1501 chars | default text, `str()` of value, cut to 1500 |
| TC-16 | Normal | prepare_worklog valid | connection checked, then approval requested with normalised form; returns approval_id |
| TC-17 | Boundary | description padded / numeric; remaining_estimate omitted | stripped / `str()`; defaults to leave |
| TC-18 | Abnormal | missing or invalid issue key, duration, date, start_time | clarify message naming the problem; no connection check, no approval |
| TC-19 | Abnormal | remaining_estimate invalid or explicit null; new without new_estimate | clarify; no approval |
| TC-20 | Boundary | lower-case issue key "proj-12" | clarify (current behaviour, spec gap GAP-10) |
| TC-21 | Abnormal | prepare_worklog for a user without Jira | ValueError from the connection check; no approval |
| TC-22 | Normal | list_assigned_issues | account id resolved, `find_assigned_issues(account_id)` |
| TC-23 | Normal | get_today_worklogs | day as YYYY-MM-DD, worklogs |
| TC-24 | Normal / Abnormal | get_issue_worklogs valid key / invalid key | worklogs for that key / ValueError, no Jira call |

## Approval actions (`handle_approval_action`)

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-25 | Normal | owner approves, worklog created | ephemeral success to the clicker; original message replaced (`chat.update`) |
| TC-26 | Abnormal | approve refused (not owner, expired, handled) | ephemeral refusal; original message **not** replaced |
| TC-27 | Exception | approve raises (Jira or DynamoDB failure) | ephemeral "check Jira before submitting again"; message not replaced; task returns 200 (no retry) |
| TC-28 | Normal | owner rejects | ephemeral "Approval rejected"; message replaced |
| TC-29 | Abnormal | reject refused | ephemeral refusal; message not replaced |
| TC-30 | Boundary | success without message_ts | ephemeral only; no `chat.update` |
| TC-31 | Exception | ephemeral fails / update fails | other notification still attempted; logged; task returns 200 |
| TC-32 | Normal / Abnormal | jira_worklog, list_assigned_issues / unknown task | routed to the read tasks / 400 |

## Slack client (`slack_api`, `post_slack_response`)

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-33 | Normal | slack_api ok | POST `https://slack.com/api/<method>`, Bearer bot token, JSON UTF-8, timeout 8; result returned |
| TC-34 | Abnormal | Slack `ok: false` with / without error | RuntimeError with the method and error (`unknown_error`); token not in message |
| TC-35 | Exception | SLACK_BOT_TOKEN unset | KeyError; no HTTP call |
| TC-36 | Normal | post_slack_response to hooks.slack.com / hooks.slack-gov.com | POST JSON, timeout 8 |
| TC-37 | Abnormal | response_url http, other host, lookalike host, userinfo trick, empty | ValueError; no HTTP call |
