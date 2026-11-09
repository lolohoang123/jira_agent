# Integration & E2E Scenarios – Jira Agent Slack Bot

- **Business workflows:** W1 worklog via the Log Time modal → approval → Jira; W2 worklog via a natural-language
  request; W3 connecting Jira and refreshing tokens; W4 read commands (`list`, `logwork today`).
- **Modules:** `functions/read_jira_lambda` → `handlers/` → `approval/`, `agent/`, `jira/`, `slack/`, `util/`.
- **Actors:** owner `U1` (connected to Jira), other workspace user `U2`, Slack platform, Lambda async
  self-invocation, Jira Cloud, Atlassian OAuth.
- **Stores:** `APPROVALS` (approval rows + `dedupe#…` rows), `OAUTH_USERS`, `OAUTH_STATES`.

These scenarios complement the unit tests (`tests/test_approval_matrix.py`, `test_gap_fixes.py`,
`test_request_path.py`, `test_approval_wire_format.py`). Unit tests mock one layer at a time; every scenario
here runs **several modules together against a real DynamoDB engine**, which is where conditional writes,
transactions and TTL-style expiry checks can actually be verified.

## 1. Test harness (how every scenario runs)

| Concern | Approach |
|---|---|
| Entry point | Call `lambda_handler(event, context)` with **signed** Slack events (`v0` HMAC with a test signing secret). |
| Async self-invoke | A fake `lambda_client` records each `invoke(Payload=…)`; the test then feeds that payload back into `lambda_handler` as the async event. This exercises the `ASYNC_TASK_SECRET` check and lets the test replay or duplicate deliveries on purpose. |
| DynamoDB | A real engine: `moto` (`mock_aws`) in-process, or DynamoDB Local via the existing `DYNAMODB_ENDPOINT_URL` support. Three tables created per test; nothing is shared between tests. |
| Slack Web API | Patch `slack.slack_client.urllib.request.urlopen` (or `slack_api`) with a recorder that returns `{"ok": true}` or a scripted error/timeout. `response_url` posts are recorded the same way. |
| Jira / Atlassian | Patch `urlopen` in `jira.jira_client`, `jira.authentication_jira` and `util.dynamo_helper` with a scripted fake: responses per URL, counters, optional delay/timeout. |
| Bedrock (W2) | Inject a fake `BedrockLLMClient` returning a fixed JSON plan; the model is never called. |
| Clock | Freeze `time.time()` and `datetime.now` per scenario; advance it to test expiry (24 h approvals, 10 min OAuth states, token expiry). |
| Assertions | Read the tables back with `ConsistentRead`; count fake calls (Jira POSTs, Slack messages, DMs); check logs contain no tokens or descriptions. |

Fixture state used below: `OAUTH_USERS[U1]` connected with a valid token, `jiraSiteUrl=https://acme.atlassian.net`;
form F = `PROJ-12`, `2026-11-09`, `09:00`, `1h 30m`, description "Code review".

## 2. Scenario matrix

### Category legend
`E2E-HAPPY` end-to-end success · `E2E-ROLLBACK` mid-flow failure and compensation · `E2E-IDEM` duplicate
submission / redelivery · `E2E-RACE` concurrent actors · `E2E-AUTHZ` access control · `E2E-EXPIRY` time-based state

| ID | Category | Actors | Step-by-step sequence | Expected DB state | Expected side-effects |
|:-:|:-:|---|---|---|---|
| IT-01 | `E2E-HAPPY` | U1 → Slack → Lambda → Jira | 1. `view_submission` with F (signed).<br>2. Replay the recorded `request_worklog_approval` task.<br>3. `block_actions` Approve by U1 with the posted `approval_id`.<br>4. Replay `handle_approval_action`. | Approval `APPROVED_EXECUTED`, `approved_at` and `completed_at` set, `ttl = now + 90 d`. Dedupe row present, `target_approval_id` = approval. | Step 1 returns `response_action: clear` within the sync call. 1 `chat.postMessage` with Approve/Reject buttons to C1. **Exactly 1** Jira `POST …/PROJ-12/worklog` with `started=2026-11-09T09:00:00.000+0700`, `timeSpent=1h 30m`. 1 ephemeral success + 1 `chat.update` replacing the buttons. |
| IT-02 | `E2E-HAPPY` | U1 → agent → approval | 1. Slash command `/jira-agent log 1h 30m on PROJ-12 today at 9:00`.<br>2. Replay `agent_request` (fake Bedrock returns `prepare_worklog` with F).<br>3. Approve as in IT-01. | Same as IT-01. | Sync reply "I'm processing…". `response_url` gets "approval request has been posted". Bedrock fake called once; Jira POST only after Approve. |
| IT-03 | `E2E-ROLLBACK` | U1 → Jira 500 | IT-01 steps 1–4, Jira fake returns `500` on the worklog POST. | Approval `EXECUTION_FAILED`, `error_message` contains "HTTP 500" and no description. **Dedupe row deleted.** | 1 Jira POST. Ephemeral "check Jira before submitting again". Original message keeps its buttons (not updated). Async task returns, Lambda does not retry. |
| IT-04 | `E2E-ROLLBACK` | U1 resubmits after IT-03 | Continue IT-03: submit F again and approve. | New approval `APPROVED_EXECUTED`; old one stays `EXECUTION_FAILED`. New dedupe row. | Second Jira POST succeeds. Proves a real failure frees the slot. |
| IT-05 | `E2E-ROLLBACK` | U1 → Slack `channel_not_found` | `view_submission` with C9 where the bot is not a member; Slack fake answers `ok: false, channel_not_found`. | Approval `CANCELLED`, `error_message` "Slack delivery failed: …". **Dedupe row deleted.** | 0 approval messages. 1 DM to U1 (`chat.postMessage channel=U1`) "couldn't post the approval … Nothing was logged to Jira", without the description. 0 Jira calls. |
| IT-06 | `E2E-ROLLBACK` | U1 → Slack timeout | As IT-05 but the Slack fake raises a timeout. | Approval stays `PENDING`; dedupe row kept. | No DM. Resubmitting F within 24 h is refused as duplicate (documented trade-off). |
| IT-07 | `E2E-ROLLBACK` | Jira OK, DynamoDB fails after | IT-01, but make `finish(APPROVED_EXECUTED)` fail once (inject a DynamoDB error after the Jira POST). | Approval `EXECUTION_FAILED` (known gap TC-29). **Dedupe row kept.** | 1 Jira POST (worklog exists). User told to check Jira. Resubmitting F is refused as duplicate for 24 h, so no second worklog. |
| IT-08 | `E2E-ROLLBACK` | Token refresh fails | `OAUTH_USERS[U1]` token expired; Atlassian token endpoint returns `400 invalid_grant`; U1 approves an approval. | Approval `EXECUTION_FAILED`. `OAUTH_USERS[U1]` refresh lock **removed** (`refreshLockOwner` absent). Tokens unchanged. | 0 Jira worklog POSTs. Logs contain no token values. |
| IT-09 | `E2E-IDEM` | U1 double-submits the modal | Two `view_submission` with F; replay both tasks. | **1** `PENDING` approval + 1 dedupe row. | 1 approval message. 1 DM "identical worklog request … already waiting". |
| IT-10 | `E2E-IDEM` | Lambda redelivers an async event | Replay the same `request_worklog_approval` payload twice (Lambda at-least-once delivery). | 1 `PENDING` approval. | 1 approval message, 1 duplicate DM (acceptable, documented). |
| IT-11 | `E2E-IDEM` | U1 clicks Approve twice | Replay `handle_approval_action` twice for the same approval. | `APPROVED_EXECUTED`, unchanged by the 2nd call. | **Exactly 1** Jira POST. 2nd call: ephemeral "expired, already handled…"; no second `chat.update`. |
| IT-12 | `E2E-IDEM` | U1 resubmits after success | After IT-01, submit F again (same day/time/duration); then with `start_time=10:00`. | First retry: no new row. Second: new `PENDING` approval. | First: duplicate DM. Second: new approval message. |
| IT-13 | `E2E-RACE` | Two Approve deliveries in parallel | Run two `handle_approval_action` replays concurrently (threads), Jira fake delayed 200 ms. | 1 `APPROVED_EXECUTED`. | **Exactly 1** Jira POST; the loser gets the refusal message. |
| IT-14 | `E2E-RACE` | U1 Approve vs U1 Reject in parallel | Approve and Reject tasks concurrently. | Exactly one of `APPROVED_EXECUTED` or `REJECTED`; never both writes. | Jira POST count = 1 if approve won, 0 if reject won; the other click gets the refusal. |
| IT-15 | `E2E-RACE` | Two requests with an expired token | Two concurrent `list` tasks for U1 with an expired access token; Atlassian refresh delayed. | `OAUTH_USERS[U1]` has the new tokens; lock removed. | **Exactly 1** call to the Atlassian token endpoint; both tasks succeed (one waits and reuses the refreshed token). |
| IT-16 | `E2E-RACE` | Two identical submissions in parallel | Run two `request_worklog_approval` tasks for F concurrently. | 1 `PENDING` approval (transaction on the dedupe row). | 1 approval message + 1 duplicate DM. |
| IT-17 | `E2E-AUTHZ` | U2 clicks U1's Approve | U2 sends `block_actions` Approve with U1's `approval_id`. | U1's approval still `PENDING`, untouched. | 0 Jira calls. Ephemeral refusal to U2 only. **No `chat.update`** (U1 keeps the buttons). |
| IT-18 | `E2E-AUTHZ` | U2 clicks U1's Reject | As IT-17 with Reject. | Still `PENDING`; dedupe row kept. | Refusal to U2; message untouched. |
| IT-19 | `E2E-AUTHZ` | Forged async event | Invoke `lambda_handler` with `{"_async_task": "handle_approval_action", …}` and a missing/wrong `_task_secret`. | Unchanged. | `403`; 0 Slack/Jira calls. |
| IT-20 | `E2E-AUTHZ` | Unsigned or replayed Slack request | Send IT-01 step 3 with a bad signature, then the original signed request 6 minutes later. | Unchanged. | `401` both times; no async invoke. |
| IT-21 | `E2E-AUTHZ` | Approval id is a dedupe key | U1 sends Approve with value `dedupe#<key>`. | Dedupe row unchanged. | Refusal; 0 Jira calls. |
| IT-22 | `E2E-AUTHZ` | U2 reuses U1's OAuth link | U1 runs `connect`; the callback is opened twice with the same `state` (second time by U2's browser). | `OAUTH_USERS[U1]` written once; no row for U2; state has `consumedAt`. | First callback 200; second 400 "expired or already used". |
| IT-23 | `E2E-AUTHZ` | Not connected user | U2 (no `OAUTH_USERS` row) runs `list`, asks the agent to log time, then submits the Log Time modal and approves it. | `list` and agent: no approval row (agent checks the connection first). **Modal: a `PENDING` row is created** and ends `EXECUTION_FAILED` on Approve. | `list`: "not connected", no invoke. Agent: generic failure reply. Modal: approval message posted, then 0 Jira calls and a failure message. **Gap:** the modal path should check the connection before creating the approval (as the agent path does). |
| IT-24 | `E2E-EXPIRY` | Approve after 24 h | Create approval, advance clock 24 h + 1 s, U1 approves. | Still `PENDING` (TTL deletes later). | 0 Jira calls; refusal. |
| IT-25 | `E2E-EXPIRY` | Resubmit after dedupe window | After IT-01, advance clock 24 h + 1 s (dedupe row still physically present), submit F. | New `PENDING` approval; dedupe row overwritten with the new `target_approval_id`. | New approval message. |
| IT-26 | `E2E-EXPIRY` | OAuth link after 10 min | `connect`, advance 601 s, open callback. | No `OAUTH_USERS` write. | 400 "expired or already used"; no token exchange call to Atlassian. |
| IT-27 | `E2E-HAPPY` | U1 reconnects to another site | Connect site A, then connect site B. | `OAUTH_USERS[U1]` holds only site B (`jiraCloudId`, `jiraSiteUrl`). | Links in `list` use site B's URL. |
| IT-28 | `E2E-HAPPY` | U1 `list` and `logwork today` | Run both commands; Jira fake returns 21 issues (with `timespent`) and today's worklogs. | No writes (except a token refresh if expired). | 1 Jira search for `list` (no per-issue worklog calls); "Showing 20 of 21"; links built from `jiraSiteUrl`; `response_url` used, not `chat.postMessage`. |

## 3. Priorities

| Priority | Scenarios | Why |
|---|---|---|
| P1 – must have | IT-01, IT-03, IT-07, IT-11, IT-13, IT-17, IT-19, IT-09 | Cover the non-negotiable rules: single write path, owner-only approval, no duplicate Jira worklogs, signed and secret-authenticated entry. |
| P2 – should have | IT-02, IT-04, IT-05, IT-06, IT-10, IT-14, IT-16, IT-20, IT-22, IT-24, IT-25 | Compensation paths and expiry rules added by the GAP fixes. |
| P3 – nice to have | IT-08, IT-12, IT-15, IT-18, IT-21, IT-23, IT-26, IT-27, IT-28 | Token lifecycle, read paths and defensive cases. |

## 4. Notes

- **Race scenarios need a real engine.** IT-13–IT-16 rely on DynamoDB's conditional writes and transactions;
  a mocked table always returns what the test says, so these can only be proven with moto or DynamoDB Local.
  `moto` is not installed yet (test-only dependency; runtime stays stdlib + boto3).
- **No retries from Lambda.** Scenarios that end in a failure also assert that the async task *returns*
  instead of raising, so a real Lambda would not redeliver it (see `handlers/async_task_handler.py`).
- **Privacy checks** apply to every scenario: captured logs and DynamoDB `error_message` must not contain
  access/refresh tokens or the worklog description.
- **Out of scope here:** real Slack, Jira and Bedrock. A short manual smoke test in a staging Slack workspace
  (IT-01, IT-17, IT-05) is still recommended before release.
