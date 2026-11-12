# Data Model & Processing Flows – Jira Agent Slack Bot

Describes the code and infrastructure as of the release-readiness work (DM approvals, Jira site binding,
disconnect / revoked tokens, per-user rate limit, SSM secrets, SAM + GitHub OIDC deploy).
Sources of truth: `functions/`, `handlers/`, `approval/`, `agent/`, `jira/`, `util/`, `template.yaml`,
`infra/github-oidc-bootstrap.yaml`. Setup steps: `docs/deploy/RELEASE_CHECKLIST.md`.

## 0. System context

```mermaid
flowchart LR
    User((Slack user)) --> Slack[Slack]
    Slack -->|POST / signed| APIGW[API Gateway HTTP API<br/>throttled]
    Atl[Atlassian OAuth] -->|GET /oauth/callback| APIGW
    APIGW --> L[Lambda jira-agent-stage]
    L -->|async self-invoke, Event, retries 0| L
    L -->|uncaught failure| DLQ[(SQS async-failures)]
    L --> DDB[(DynamoDB<br/>oauth-states, users,<br/>approvals, rate-limits)]
    L -->|GetParameters, cached 5 min| SSM[(SSM Parameter Store<br/>/jira-agent/stage/)]
    L -->|Converse: JSON plan only| BR[Amazon Bedrock]
    L -->|REST with user token| Jira[Jira Cloud API]
    L -->|chat.postMessage, views.open, response_url| SlackAPI[Slack Web API]
    L --> CW[CloudWatch Logs and alarms]
```

**One Lambda, three entry types** (`functions/read_jira_lambda.py`):

| Entry | Recognised by | Authentication | Routed to |
|---|---|---|---|
| OAuth callback | `GET …/oauth/callback` | one-time `state` stored in DynamoDB | `jira/authentication_jira.py` |
| Async task | event has `_async_task` | `_task_secret` equals `ASYNC_TASK_SECRET` (constant-time), else 403 | `handlers/async_task_handler.py` |
| Slack request | anything else | Slack v0 HMAC signature, timestamp within 300 s, else 401 | `handlers/slack_command_handler.py` |

Slack must receive an answer within 3 s, so every slow operation (Bedrock, Jira, Slack posts) runs in an
async self-invocation: the synchronous call validates and acknowledges, the async call does the work.

Secrets (`SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN`, `ATLASSIAN_CLIENT_SECRET`, `ASYNC_TASK_SECRET`) are read only via
`util/config.get_secret`: from SSM SecureString `/jira-agent/<stage>/<kebab-name>` in AWS (one decrypted
`GetParameters` call, cached `SECRET_CACHE_SECONDS`), from environment variables locally.

## 1. Data model (DynamoDB)

Four tables, `jira-agent-<stage>-<name>`, each with a single string partition key, on-demand capacity and
encryption at rest. There are **no GSIs, no sort keys and no scans**: every read and write is by primary key.

> **Deviation from the generic template.** No `id / version / created_at / updated_at / deleted_at` columns:
> concurrency uses **conditional writes and transactions**, deletion uses **TTL**, and relations are
> **logical only** (dashed below).

```mermaid
erDiagram
    OAUTH_USERS ||..o{ APPROVALS : "owns (slack_user_id)"
    OAUTH_USERS ||..o{ OAUTH_STATES : "connect attempts (slackUserId)"
    OAUTH_USERS ||..o{ RATE_LIMITS : "agent requests per hour"
    INSTALLATIONS ||..o{ OAUTH_USERS : "workspace (team_id)"
    APPROVALS ||..o| DEDUPE_ROW : "blocks duplicates (dedupe_key)"

    OAUTH_USERS {
        string team_id PK "Slack workspace id"
        string slack_user_id PK "Slack user id (sort key)"
        string jiraAccountId "Atlassian accountId"
        string jiraCloudId "Jira site cloud id"
        string jiraSiteUrl "Used to build issue links"
        string jiraSiteName
        string timeZone "IANA tz: Jira /myself, else Slack users.info; backfilled from Jira if missing"
        string accessToken "Secret, never logged, removed on revoke"
        string refreshToken "Secret, rotated on refresh, removed on revoke"
        number accessTokenExpiresAt "Unix seconds (old rows: ms)"
        string accessTokenExpiresAtIso
        string scope
        bool connected "false after invalid_grant"
        number connectedAt
        number disconnectedAt "set when tokens were revoked"
        number updatedAt
        string refreshLockOwner "Refresh lock, uuid"
        number refreshLockUntil "Lock expiry, now + 30s"
    }
    OAUTH_STATES {
        string state PK "token_urlsafe(32), one-time"
        string slackUserId
        number createdAt
        number expiresAt "TTL attribute, now + 600s"
        number consumedAt "Set once, makes the state single-use"
    }
    APPROVALS {
        string approval_id PK "uuid4"
        string team_id "Owner workspace"
        string slack_user_id "Owner, the only user who may approve (with team_id)"
        string channel_id "Channel the request came from"
        string action "create_worklog"
        map form "issue_key, date, start_time, duration, description, remaining_estimate, new_estimate"
        string jira_cloud_id "Jira site at request time; the write is refused for any other site"
        string status "PENDING, EXECUTING, APPROVED_EXECUTED, EXECUTION_FAILED, REJECTED, CANCELLED"
        string dedupe_key "sha256 of user, issue, date, start, duration"
        number created_at
        number expires_at "Approve deadline, created + 24h"
        number approved_at
        number rejected_at
        number completed_at
        string error_message "Max 500 chars, no description"
        number ttl "TTL attribute, 24h while PENDING, + retention once handled"
    }
    INSTALLATIONS {
        string team_id PK "Slack workspace id"
        string team_name
        string bot_token "Secret, never logged"
        string bot_user_id
        string app_id
        string scope
        string installed_by_user_id
        number installed_at
    }
    DEDUPE_ROW {
        string approval_id PK "dedupe-prefixed key, same table as APPROVALS"
        string target_approval_id "Approval that holds the slot"
        number expires_at "created + 24h"
        number ttl "TTL attribute, created + 24h"
    }
    RATE_LIMITS {
        string limit_key PK "agent, slack user id and window start"
        number request_count "atomic ADD"
        number ttl "TTL attribute, window start + 2h"
    }
```

`DEDUPE_ROW` lives in the approvals table with key `dedupe#<dedupe_key>`. It has no `status`, so it can never
satisfy the claim or reject conditions even if its key were sent as an approval id.

### Access patterns and conditions

| Table | Operation | Condition / guarantee | Code |
|---|---|---|---|
| `oauth-states` | Put state | `attribute_not_exists(state)` | `DynamoHelper.create_oauth_authorization_url` |
| `oauth-states` | Consume state | exists, `consumedAt` not set, `expiresAt > now` (single use) | `DynamoHelper.consume_oauth_state` |
| `users` | Put connection | unconditional: reconnecting replaces the previous site (one site per user) | `AuthenticationJira.finish_oauth_callback` |
| `users` | Read account id / cloud id | `ConsistentRead`; projection of that attribute plus `connected` only (never tokens); `connected = false` counts as not connected | `DynamoHelper._read_connection` |
| `users` | Read credentials | `ConsistentRead`; refresh when the token expires within 60 s | `DynamoHelper.get_oauth_credentials` |
| `users` | Take refresh lock | no lock, lock expired, or we own it | `DynamoHelper.refresh_access_token` |
| `users` | Store refreshed tokens | `refreshLockOwner = me`, removes the lock | `DynamoHelper.refresh_access_token` |
| `users` | Forget revoked tokens | `refreshLockOwner = me`; removes tokens and lock, sets `connected = false` | `DynamoHelper._forget_tokens` |
| `users` | Disconnect | delete item (`ReturnValues=ALL_OLD`) | `DynamoHelper.disconnect_slack_user` |
| `approvals` | Create approval + dedupe row | **transaction**: new approval id, and no live dedupe row (`attribute_not_exists OR expires_at <= now`) | `ApprovalRepository.create_pending` |
| `approvals` | Claim / reject | `status = PENDING AND slack_user_id = caller AND expires_at > now`; extends `ttl` to retention | `ApprovalRepository.claim`, `reject` |
| `approvals` | Cancel undelivered | `status = PENDING` | `ApprovalRepository.cancel` |
| `approvals` | Finish (incl. CANCELLED on site change) | `status = EXECUTING`; extends `ttl` to retention | `ApprovalRepository.finish` |
| `approvals` | Release dedupe row | `target_approval_id = this approval` (never frees a newer request's slot) | `ApprovalRepository.release_dedupe` |
| `rate-limits` | Count a request | `ADD count`, condition `attribute_not_exists(count) OR count < limit`; failure = limited; other errors fail open | `RateLimiter.allow` |

### Retention

| Item | Removed by | When |
|---|---|---|
| OAuth state | TTL on `expiresAt` | 10 min after creation (single use even before that) |
| Jira connection and tokens | `/jira-agent disconnect` (row deleted), or `invalid_grant` (tokens deleted, row kept as disconnected) | on demand |
| Pending approval | TTL on `ttl` | 24 h after creation if never handled |
| Handled approval | TTL on `ttl` | `APPROVAL_RETENTION_DAYS` (default 90) after the last status change (audit trail) |
| Dedupe row | TTL, or deleted on reject / cancel / Jira failure | 24 h after creation |
| Rate-limit counter | TTL on `ttl` | 2 h after the window started |
| Failed async payload | SQS retention | 4 days (encrypted; contains the task secret and the form) |
| Logs | CloudWatch retention | `LogRetentionDays` (default 30) |

DynamoDB TTL deletes lazily (usually within 48 h), so every expiry is also enforced in a condition
(`expires_at > now`, `expiresAt > now`) instead of relying on the row being gone.

## 2. Approval state machine

```mermaid
stateDiagram-v2
    [*] --> PENDING : create_pending (with dedupe row and jira_cloud_id)
    PENDING --> EXECUTING : claim by owner before expires_at
    PENDING --> REJECTED : reject by owner before expires_at
    PENDING --> CANCELLED : Slack definitely rejected the approval message
    PENDING --> Expired : expires_at passed, buttons refuse
    EXECUTING --> CANCELLED : Jira site changed since the request
    EXECUTING --> APPROVED_EXECUTED : Jira worklog created
    EXECUTING --> EXECUTION_FAILED : Jira error or unsupported action
    APPROVED_EXECUTED --> [*]
    EXECUTION_FAILED --> [*]
    REJECTED --> [*]
    CANCELLED --> [*]
    Expired --> [*] : TTL deletes the row
```

- Only `PENDING → EXECUTING` leads to a Jira write, and only the async task `handle_approval_action` performs it.
- **Site binding:** the approval stores the user's `jira_cloud_id` at request time. After the claim, the service
  compares it with the cloud id of the credentials it will write with; a reconnect to another site ends the
  approval as `CANCELLED` without writing. Approvals created before this field existed are not checked.
- **Dedupe row:** released on `REJECTED`, `CANCELLED` (undelivered) and `EXECUTION_FAILED` when Jira did not write,
  so the user can resubmit. Kept after `APPROVED_EXECUTED`, and kept when Jira accepted the worklog but the status
  update failed (known gap TC-29 / GAP-07: the row ends `EXECUTION_FAILED` although the worklog exists).
- **Slack timeout** while sending the approval leaves it `PENDING` (the message may have been delivered).
- `Expired` is not stored: the row stays `PENDING` until TTL removes it, and the time condition refuses any click.

## 3. Layer structure

```mermaid
classDiagram
    direction LR
    class lambda_handler {
        +route(event) response
    }
    class SlackCommandHandler {
        +handle_slack_request()
        -_disconnect()
    }
    class SlackActionHandler {
        +handle_block_actions()
        +handle_view_submission()
    }
    class AsyncTaskHandler {
        +handle_async_task(event)
    }
    class RateLimiter {
        +allow(key, limit) bool
    }
    class AgentOrchestrator {
        +handle(user, text, channel) AgentResult
    }
    class BedrockLLMClient {
        +generate_plan(prompt, text) dict
    }
    class ToolRegistry {
        +execute_plan(plan, user, channel) dict
    }
    class ApprovalService {
        +request_worklog_approval()
        +approve_and_execute()
        +reject()
    }
    class ApprovalRepository {
        +create_pending()
        +claim()
        +reject()
        +cancel()
        +finish()
        +release_dedupe()
    }
    class JiraService {
        +find_assigned_issues()
        +fetch_today_worklogs()
        +create_worklog()
    }
    class JiraClient {
        +request(path)
    }
    class DynamoHelper {
        +get_oauth_credentials()
        +refresh_access_token()
        +consume_oauth_state()
        +disconnect_slack_user()
    }
    class Config {
        +get_secret(name)
    }
    lambda_handler --> SlackCommandHandler : signed Slack request
    lambda_handler --> AsyncTaskHandler : async event + secret
    SlackCommandHandler --> SlackActionHandler : interactivity payload
    SlackCommandHandler --> RateLimiter : agent requests only
    AsyncTaskHandler --> AgentOrchestrator : agent_request
    AsyncTaskHandler --> ApprovalService : approval tasks
    AgentOrchestrator --> BedrockLLMClient : plan only
    AgentOrchestrator --> ToolRegistry : validated plan
    ToolRegistry --> ApprovalService : prepare_worklog
    ToolRegistry --> JiraService : read intents
    ApprovalService --> ApprovalRepository
    ApprovalService --> JiraService : write after claim and site check
    JiraService --> JiraClient
    JiraClient --> DynamoHelper : tokens
    lambda_handler ..> Config : signing and task secrets
    DynamoHelper ..> Config : Atlassian client secret
```

The model never reaches `JiraService` directly: `ToolRegistry` validates the plan against `ALLOWED_INTENTS`,
and the only write it can trigger is creating a PENDING approval.

## 4. Sequence diagrams

### 4.1 Connect, token refresh and disconnect

```mermaid
sequenceDiagram
    autonumber
    actor U as Slack user
    participant SL as Slack
    participant L as Lambda
    participant ST as oauth-states
    participant AT as Atlassian
    participant US as users

    U->>SL: /jira-agent connect
    SL->>L: POST / slash command (signed)
    L->>ST: Put state (expiresAt = now + 600)
    L-->>SL: Ephemeral Connect Jira link
    U->>AT: Open link, grant consent
    AT->>L: GET /oauth/callback with code and state
    L->>ST: Consume state (exists, unused, unexpired)
    alt State invalid, expired or reused
        L-->>U: 400 page "link expired or already used"
    else State valid
        L->>AT: Exchange code, list sites, read /myself
        alt No refresh token, no site, or several sites without JIRA_BASE_URL
            L-->>U: 500 page "connection failed" (details only in logs)
        else OK
            L->>US: Put connection (replaces previous site)
            L-->>U: 200 page "Jira connected"
        end
    end

    Note over L,US: Later, any Jira call: token expires within 60 s
    L->>US: Take refresh lock (conditional)
    L->>AT: refresh_token grant
    alt New tokens
        L->>US: Store tokens and release lock (condition: lock owner)
    else invalid_grant, unauthorized_client or access_denied
        L->>US: Remove tokens and lock, connected = false
        L-->>SL: "authorization expired or revoked, run /jira-agent connect"
    end

    U->>SL: /jira-agent disconnect
    SL->>L: POST / slash command (signed)
    L->>US: Delete connection (tokens included)
    L-->>SL: Ephemeral "disconnected" (revoke at Atlassian under Connected apps)
```

### 4.2 Read commands: `list` and `logwork today`

```mermaid
sequenceDiagram
    autonumber
    actor U as Slack user
    participant SL as Slack
    participant L as Lambda (sync)
    participant A as Lambda (async)
    participant J as Jira REST API

    U->>SL: /jira-agent list
    SL->>L: POST / slash command (signed)
    L->>L: Connected? (account id and connected flag)
    alt Not connected
        L-->>SL: "Run /jira-agent connect first"
    else Connected
        L->>A: invoke Event list_assigned_issues (task secret)
        L-->>SL: "Fetching your Jira data..." (within 3 s)
        A->>J: search assignee = account (fields summary, status, timespent)
        A-->>SL: POST response_url: up to 20 issues, totals, links from jiraSiteUrl, Log Time buttons
    end
```

`logwork today` follows the same path with task `jira_worklog`: issues with worklogs today, then each issue's
worklogs filtered to the user and to today in the user's timezone (`timeZone`, else `JIRA_TIMEZONE`) (at most 23 shown; Slack allows 50 blocks).

`logwork find <keyword>` (alias `find <keyword>`) follows the same path with task `search_issues`:
- the keyword is reduced to plain terms (`jql_text_terms`: quotes, backslashes and JQL/Lucene operators removed,
  100 characters max), so user text can never change the JQL structure; an empty result gets a usage hint;
- LIKE-style matching (`jql_like_clause`): each word (at most 5) becomes a prefix match and all are required,
  `logi bu` → `text ~ "logi*" AND text ~ "bu*"`; one-character words are matched exactly. JQL has no SQL LIKE and
  Jira only allows a trailing wildcard, so "gin" cannot match "login";
- one search `<clause> ORDER BY updated DESC` with `maxResults` 21 (20 shown, the 21st only signals
  "more than 20 match"); if the keyword is an issue key it is also fetched directly and listed first;
- each row shows the link, status, total logged time (`timespent`) and a **Log Time** button that opens the
  existing modal, so the worklog still goes through the approval flow (4.3, 4.4).

### 4.3 Log Time modal → approval request (DM)

```mermaid
sequenceDiagram
    autonumber
    actor U as Slack user
    participant SL as Slack
    participant L as Lambda (sync)
    participant A as Lambda (async)
    participant S as ApprovalService
    participant DB as approvals table

    U->>SL: Submit Log Time modal
    SL->>L: POST / view_submission (signed)
    L->>L: Validate issue key, date, start time, 0 < duration <= 24h, estimate
    alt Validation fails
        L-->>SL: 200 response_action errors (modal stays open)
    else Valid
        L->>A: invoke Event request_worklog_approval
        L-->>SL: 200 response_action clear (within 3 s)
        A->>S: request_worklog_approval(user, channel, form)
        S->>S: Read the user's current jira_cloud_id
        rect rgb(240, 249, 255)
            Note over S,DB: Transaction: approval PENDING (with jira_cloud_id) + dedupe row
            S->>DB: TransactWriteItems
        end
        alt Identical request pending or approved in last 24 h
            DB-->>S: TransactionCanceled (dedupe condition)
            A->>SL: DM to user "identical request already exists"
        else Created
            S->>SL: chat.postMessage to the user (DM) with Approve and Reject
            alt Slack rejects (ok false or HTTP error)
                rect rgb(254, 242, 242)
                    Note over S,DB: Compensation: PENDING to CANCELLED, release dedupe row
                    S->>DB: Cancel and delete dedupe row
                end
                A->>SL: DM "could not send the approval, nothing logged"
            else Slack timeout
                Note over S,DB: Keep PENDING (message may have been delivered)
            else Delivered
                SL-->>U: Approval DM (private to the requester)
            end
        end
    end
```

With `APPROVAL_DELIVERY=channel` the approval message is posted in the originating channel instead of a DM.

### 4.4 Approve → Jira worklog (the only write path)

```mermaid
sequenceDiagram
    autonumber
    actor U as Clicking user
    participant SL as Slack
    participant L as Lambda (sync)
    participant A as Lambda (async)
    participant S as ApprovalService
    participant DB as approvals table
    participant J as Jira REST API

    U->>SL: Click Approve
    SL->>L: POST / block_actions approve_jira_write (signed)
    L->>A: invoke Event handle_approval_action
    L-->>SL: 200 empty (within 3 s)
    A->>S: approve_and_execute(approval_id, user)
    S->>DB: Claim PENDING to EXECUTING if owner and not expired
    alt Not owner, expired or already handled
        A->>SL: Ephemeral refusal (original message untouched)
    else Claimed
        S->>S: Load credentials, compare cloud id with jira_cloud_id
        alt Jira site changed
            S->>DB: finish CANCELLED (nothing written)
            A->>SL: Ephemeral "Jira connection now points to a different site"
        else Same site
            S->>J: POST issue worklog (started with offset, timeSpent, ADF comment, adjustEstimate)
            alt Jira success
                S->>DB: finish APPROVED_EXECUTED, ttl = now + retention
                A->>SL: Ephemeral success and chat.update replaces the buttons
            else Jira error or timeout
                rect rgb(254, 242, 242)
                    Note over S,DB: finish EXECUTION_FAILED, release dedupe row
                    S->>DB: Update and delete dedupe row
                end
                A->>SL: Ephemeral "check Jira before submitting again" (no retry)
            end
        end
    end
```

### 4.5 Natural-language request (agent)

> **Currently disabled** by the feature flag `AGENT_ENABLED=false` (template parameter `AgentEnabled`, default `false`).
> Free text is answered synchronously with the list of available commands, before the rate limit and without an async
> invoke; `agent_request` tasks already queued are answered the same way without calling Bedrock. The flow below applies
> when the flag is `true`.

```mermaid
sequenceDiagram
    autonumber
    actor U as Slack user
    participant SL as Slack
    participant L as Lambda (sync)
    participant RL as rate-limits table
    participant A as Lambda (async)
    participant O as AgentOrchestrator
    participant B as Bedrock
    participant T as ToolRegistry

    U->>SL: /jira-agent log 1h on PROJ-12 today at 9:00
    SL->>L: POST / slash command (signed)
    L->>RL: Count request for this user and hour
    alt Limit reached (default 20 per hour)
        L-->>SL: "limit of 20 assistant requests per hour"
    else Allowed (or DynamoDB error: fail open)
        L->>A: invoke Event agent_request
        L-->>SL: "I'm processing your Jira request..."
        A->>O: handle(user, text, channel)
        O->>B: Converse (system prompt + date, time and timezone)
        B-->>O: JSON plan only
        O->>T: execute_plan(plan)
        alt Intent not allow-listed or invalid parameters
            T-->>O: refused or clarify message
        else Read intent
            T->>T: JiraService read (list, today, issue worklogs)
        else prepare_worklog
            T->>T: Validate form, check connection, ApprovalService (flow 4.3 from the transaction on)
        end
        A-->>SL: POST response_url (ephemeral reply)
    end
```

## 5. Processing steps – worklog from request to Jira

| Step | Layer | Action | Exception / branch | Result & compensation |
|:-:|---|---|---|---|
| 1 | `lambda_handler` | Verify Slack signature and timestamp (±300 s) | Bad or missing signature | `401 invalid Slack signature` |
| 2 | `SlackActionHandler` / `ToolRegistry` | Validate form: issue key, date, start time, `0 < duration <= MAX_WORKLOG_MINUTES`, estimate | Invalid field | Modal: errors per block. Agent: clarify. Nothing stored |
| 3 | `SlackActionHandler` | Invoke async task, ACK Slack | Invoke throttled (account concurrency) | `500` to Slack (`dispatch_failed`); throttles alarm |
| 4 | `ApprovalRepository` (transaction) | Create PENDING approval (with `jira_cloud_id`) and dedupe row | Live dedupe row exists | `DuplicateApprovalError` → DM, nothing stored |
| 5 | `ApprovalService` | Send Approve/Reject (DM by default) | Slack error / Slack timeout | Cancel + release dedupe + DM / keep PENDING |
| 6 | `ApprovalRepository` | Owner clicks Approve: conditional claim to EXECUTING | Not owner, expired, already handled | Ephemeral refusal, row unchanged |
| 7 | `ApprovalService` | Compare current Jira site with `jira_cloud_id` | Site changed | `CANCELLED`, nothing written |
| 8 | `JiraService` | POST worklog | Jira error or timeout | `EXECUTION_FAILED`, dedupe released, user told to check Jira; not retried |
| 9 | `ApprovalRepository` | Finish `APPROVED_EXECUTED`, `ttl` = retention | DynamoDB error after Jira success | `EXECUTION_FAILED`, dedupe **kept** (TC-29) |
| 10 | `AsyncTaskHandler` | Notify user; replace buttons only on success | Slack error | Logged, best effort |

## 7. Multi-workspace (Slack OAuth v2)

Every Slack identity is the pair (`team_id`, `slack_user_id`): Slack user ids are only unique inside a workspace.
Jira connections (`jira-agent-<stage>-jira-connections`, PK `team_id`, SK `slack_user_id`), approvals (owner check
`slack_user_id = :owner AND team_id = :team`), dedupe keys, rate-limit keys and OAuth states all carry `team_id`.
Slack calls use the bot token of the request's workspace: `slack_api(method, payload, team_id)`.

```mermaid
sequenceDiagram
    autonumber
    actor A as Workspace admin
    participant L as Lambda
    participant ST as oauth-states
    participant SL as Slack
    participant IN as installations

    A->>L: GET /slack/install
    L->>ST: Put state (kind slack_install, 10 min)
    L-->>A: 302 to slack.com/oauth/v2/authorize (client_id, scopes, redirect_uri, state)
    A->>SL: Approve the app for the workspace
    SL->>L: GET /slack/oauth/callback with code and state
    L->>ST: Consume state (single use, unexpired, kind slack_install)
    L->>SL: oauth.v2.access (client id and secret)
    alt Enterprise Grid org-wide install
        L-->>A: 400 page "install per workspace"
    else Workspace bot token
        L->>IN: Put installation (team_id, bot token)
        L-->>A: 200 page "Jira app installed"
    end
```

```mermaid
sequenceDiagram
    autonumber
    participant SL as Slack
    participant L as Lambda (sync)
    participant A as Lambda (async)
    participant IN as installations
    participant JC as jira-connections
    participant AP as approvals

    SL->>L: POST /slack/events (signed) app_uninstalled or tokens_revoked
    L->>A: invoke Event uninstall_workspace (team_id, reason)
    L-->>SL: 200 within 3 s
    A->>IN: Delete installation
    alt app_uninstalled
        A->>JC: Query team_id, delete every connection and token
        A->>AP: Scan PENDING of team_id, cancel each (dedupe released)
    else tokens_revoked with bot tokens
        Note over A,JC: Installation only, Jira connections kept for a reinstall
    end
```

- A command from a workspace without an installation gets "hasn't installed the Jira app yet" with the install link.
- `url_verification` echoes the challenge; other events are acknowledged and ignored; events for another `api_app_id`
  are ignored when `SLACK_APP_ID` is set. Cleanup is idempotent, so Slack retries are harmless; a failed step still
  lets the others run and then fails the task into the async-failures queue.

## 6. Deployment and operations

```mermaid
flowchart LR
    Dev[push or PR] --> CI[CI: pytest, guardrails G1-G9, cfn-lint]
    Main[push to main] --> DS[Deploy job, environment staging]
    Manual[Run workflow stage=prod] --> Rev{Required reviewer}
    Rev --> DP[Deploy job, environment prod]
    DS -->|OIDC sub repo:owner@id/repo@id:environment:staging| RS[Role jira-agent-github-deploy-staging]
    DP -->|OIDC sub ...:environment:prod| RP[Role jira-agent-github-deploy-prod]
    RS --> SAM1[sam build and sam deploy<br/>stack jira-agent-staging]
    RP --> SAM2[sam build and sam deploy<br/>stack jira-agent-prod]
    SAM1 --> FR[Function role<br/>with boundary jira-agent-function-boundary]
    SAM2 --> FR
```

| Concern | How it is handled |
|---|---|
| Infrastructure | `template.yaml` (SAM). The HTTP API is declared with plain API Gateway v2 resources (Api, Integration, Routes, Stage, Lambda permission) so the function can reference the API id without a circular dependency |
| Deploy credentials | GitHub OIDC, no long-lived AWS keys. Trust is pinned to the immutable-id subject `repo:lolohoang123@6188421/jira_agent@1445373052:environment:<stage>` (`infra/github-oidc-bootstrap.yaml`) |
| Privilege escalation | Deploy roles may only create IAM roles carrying `jira-agent-function-boundary`, may never remove it, and may pass roles only to Lambda |
| Secrets | SSM SecureString under `/jira-agent/<stage>/`, created out of band; never in the template, GitHub or Lambda environment |
| Async failures | `MaximumRetryAttempts = 0` (tasks are not idempotent); uncaught failures go to the encrypted SQS queue (4 days) |
| Concurrency | `ReservedConcurrency` optional (default 0): the account limit is 10 and AWS keeps at least 10 unreserved. Raise the quota before opening prod widely |
| Alarms (SNS) | Lambda errors, throttles, p95 duration > 45 s, messages in the async-failures queue |
| Data safety | users and approvals tables: point-in-time recovery and `DeletionPolicy: Retain`. A failed **first** create leaves the retained tables behind; delete them (after checking they are empty) before redeploying |
| Invariants pinned by tests | `tests/test_infrastructure.py` (incl. a circular-dependency check that models SAM's implicit edges) and the guardrails skill |
