# Spec Coverage Audit / Báo cáo Kiểm tra Độ bao phủ Spec

- **Target document**: [data_model_and_flows.md](data_model_and_flows.md) (+ `README.md` infra sections)
- **Audit date**: 2026-11-10
- **Audit gate status**: `CONDITIONAL PASS` (the only High gap, SC-01, is fixed; Medium/Low remain open)
- **Summary**: `Critical: 0 | High: 1 (fixed) | Medium: 7 | Low: 4`

New gaps use the `SC-xx` prefix so they do not collide with the existing `GAP-01…14` references in the spec and tests.

## Detected gaps / Danh sách thiếu sót

| ID | Axis | Severity | Feature / field | Missing scenario | Recommended default |
| :-: | :-: | :-: | :--- | :--- | :--- |
| SC-01 | `SEC` | **High – FIXED** (`tests/test_site_binding.py`) | `APPROVALS.form` / claim | The approval row does not record which Jira site it was made for. If the user reconnects to a different site between request and Approve, the worklog is written to the new site (`JiraClient` always uses the current connection). | Store `jira_cloud_id` on the approval at creation. At claim, add the condition `jira_cloud_id = current cloudId`; on mismatch set `CANCELLED` and notify the user. Optionally cancel the user's PENDING approvals on reconnect. |
| SC-02 | `BND` | Medium | Worklog `date` / `start_time` | The modal only checks that date and time are present. Neither path rejects future dates, very old dates, or a start + duration that runs past midnight. | Require `today − 30d ≤ date ≤ today` in `JIRA_TIMEZONE`, and `start + duration ≤ now` for today. Make the window configurable (`WORKLOG_MAX_PAST_DAYS`). |
| SC-03 | `BND` | Medium | Daily total | Each worklog is capped at 24 h, but several 24 h worklogs on the same day are allowed. | Before creating the approval, add today's worklog total (`fetch_today_worklogs`). Refuse above 24 h, or warn on the approval message above 8 h. |
| SC-04 | `BND` | Medium | `description` | No maximum length. Slack section text is capped at 3000 chars, so a long description makes `chat.postMessage` fail and the approval ends `CANCELLED` with a confusing message. | `max_length: 1000` on the modal input, the same check in `_validate_worklog_form`, and a 300-char preview + "…" in the approval message. |
| SC-05 | `ERR` | Medium | Async task budget | The Lambda timeout is not specified against Jira (20 s), Bedrock and Slack calls. Exceptions are swallowed, so a Lambda timeout is the only trigger for Lambda's 2 automatic async retries. A retry can produce a false "identical request already exists" DM or a second Bedrock call. | Document Lambda timeout ≥ 60 s. Set async invoke config `MaximumRetryAttempts=0` with an on-failure destination (SQS DLQ). Give Bedrock an explicit read timeout (e.g. 25 s). |
| SC-06 | `ERR` | Medium | Jira / Atlassian errors | Behaviour on Jira `429` (Retry-After) and on a revoked refresh token (`invalid_grant`) is undefined. Today the user gets a RuntimeError message and `connected` stays `true`. | On 429: no retry in the write path; message "Jira is rate-limiting, try again in N s". On refresh `400/401`: set `connected=false`, clear tokens, prompt `/jira-agent connect`. |
| SC-07 | `ERR` | Medium | Flow 4.4 (agent) | Only "intent not allow-listed / invalid params" is shown. Bedrock throttling, timeout, malformed JSON and empty output have no defined user message. | Add an `alt` branch: any Bedrock failure returns "I couldn't understand that right now, try again or use the Log Time modal". Log the error class only, never the prompt. |
| SC-08 | `OPS` | Medium | `APPROVALS` audit row | The Jira `worklogId` is not stored, so a worklog cannot be traced, reconciled (GAP-07) or undone later. | Store `jira_worklog_id` and `jira_cloud_id` at `finish APPROVED_EXECUTED`. For GAP-07 recovery, look up worklogs by `started` + author to reconcile. |
| SC-09 | `SEC` | Medium | Slash command / agent | No per-user rate limit, so Bedrock cost and approval spam are unbounded. | 10 agent requests per user per minute (conditional counter on `OAUTH_USERS` or a TTL row). Excess requests get an ephemeral "slow down". |
| SC-10 | `STA` | Low | Expired PENDING | After `expires_at` the Approve/Reject buttons stay visible in the channel. Clicks are refused, but the message never shows "expired". | When a click is refused because the approval expired, also `chat.update` the message to "Expired – submit again". |
| SC-11 | `I18N` | Low | Timezone | **Resolved:** timezone is per user (`timeZone` on the Jira connection: Jira `/myself`, else Slack `users.info.tz`, else `JIRA_TIMEZONE`). Remaining: the modal and approval message do not show the timezone. | Show the timezone on the modal and approval message. |
| SC-12 | `PERF` | Low | List assigned issues | Search stops at 10×100 issues and the worklog view at 23 items; what the user sees past those caps is not specified. | Document the caps. Show "Showing X of ≥Y, refine with a project filter" whenever a cap is hit. |

Already tracked in the spec (not re-counted): GAP-04 (no disconnect), GAP-07 (Jira write succeeded but status write failed), GAP-10 (lower-case issue key in the agent), GAP-14 (async invoke throttled).

## Verified coverage (8 axes)

- [x] `BND` – duration 0 < d ≤ 24 h, issue-key pattern (gaps SC-02/03/04)
- [x] `ERR` – Slack delivery compensation, Jira failure → `EXECUTION_FAILED` + dedupe release (gaps SC-05/06/07)
- [x] `CON` – transactional dedupe, conditional `PENDING → EXECUTING` claim, refresh lock, single-use OAuth state
- [x] `SEC` – owner-only approve/reject, signature check, async secret, no token/description logging (gaps SC-01/09)
- [x] `STA` – full state machine; TTL used instead of soft delete, with the reason documented (gap SC-10)
- [ ] `I18N` – partial: single global timezone (gap SC-11)
- [ ] `PERF` – partial: caps not specified (gap SC-12)
- [x] `OPS` – 90-day audit retention (gap SC-08)

SC-01 fix: the approval stores `jira_cloud_id` at creation; after the claim it is compared with the cloud id of
the credentials that perform the write, and a mismatch ends the approval `CANCELLED` without a Jira call.
Approvals created before the fix have no `jira_cloud_id` and are not checked (they expire within 24 h).
