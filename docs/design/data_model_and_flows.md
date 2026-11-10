# Data Model & Processing Flows – Jira Agent Slack Bot

Describes the code as of the GAP-01/02/05/08 fixes. Source of truth: `approval/`, `util/dynamo_helper.py`,
`jira/authentication_jira.py`, `handlers/`, `agent/tool_registry.py`.

## 1. Data model (DynamoDB)

Three tables, each with a single string partition key and on-demand capacity. There are **no GSIs, no sort
keys and no scans**: every read and write is by primary key.

> **Deviation from the house template.** The template's `id / version / created_at / updated_at / deleted_at`
> columns do not apply:
> - concurrency control uses **conditional writes** (`ConditionExpression`) and transactions instead of a
>   `version` column;
> - deletion is done by **DynamoDB TTL** (`ttl`, `expiresAt`) instead of soft delete (`deleted_at`);
> - relations are **logical only** (DynamoDB has no foreign keys), shown dashed below.

```mermaid
erDiagram
    OAUTH_USERS ||..o{ APPROVALS : "owns (slack_user_id)"
    OAUTH_USERS ||..o{ OAUTH_STATES : "connect attempts (slackUserId)"
    APPROVALS ||..o| DEDUPE_ROW : "blocks duplicates (dedupe_key)"

    OAUTH_USERS {
        string slack_user_id PK "Slack user id"
        string jiraAccountId "Atlassian accountId"
        string jiraCloudId "Jira site cloud id"
        string jiraSiteUrl "Used to build issue links"
        string jiraSiteName
        string accessToken "Secret, never logged"
        string refreshToken "Secret, rotated on refresh"
        number accessTokenExpiresAt "Unix seconds (old rows: ms)"
        string accessTokenExpiresAtIso
        string scope
        bool connected
        number connectedAt
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
        string slack_user_id "Owner, the only user who may approve"
        string channel_id "Where the approval message is posted"
        string action "create_worklog"
        map form "issue_key, date, start_time, duration, description, remaining_estimate, new_estimate"
        string status "PENDING, EXECUTING, APPROVED_EXECUTED, EXECUTION_FAILED, REJECTED, CANCELLED"
        string jira_cloud_id "Jira site at request time, checked before the write (SC-01)"
        string dedupe_key "sha256 of user, issue, date, start, duration"
        number created_at
        number expires_at "Approve deadline, created + 24h"
        number approved_at
        number rejected_at
        number completed_at
        string error_message "Max 500 chars, no description"
        number ttl "TTL attribute, 24h while PENDING, + retention once handled"
    }
    DEDUPE_ROW {
        string approval_id PK "dedupe-prefixed key, same table as APPROVALS"
        string target_approval_id "Approval that holds the slot"
        number expires_at "created + 24h"
        number ttl "TTL attribute, created + 24h"
    }
```

`DEDUPE_ROW` is not a separate table: it lives in `APPROVALS_TABLE` with key `dedupe#<dedupe_key>`. It has
no `status`, so it can never satisfy the claim/reject conditions even if its key were clicked as an
approval id.

### Access patterns and conditions

| Table | Operation | Key | Condition / guarantee | Code |
|---|---|---|---|---|
| `OAUTH_STATES` | Put state | `state` | `attribute_not_exists(state)` | `DynamoHelper.create_oauth_authorization_url` |
| `OAUTH_STATES` | Consume state | `state` | exists, `consumedAt` not set, `expiresAt > now` (single use) | `DynamoHelper.consume_oauth_state` |
| `OAUTH_USERS` | Put connection | `slack_user_id` | Unconditional put: reconnecting replaces the old site (one site per user) | `AuthenticationJira.finish_oauth_callback` |
| `OAUTH_USERS` | Read account / credentials | `slack_user_id` | `ConsistentRead` | `DynamoHelper.get_jira_account_id_for_slack_user`, `get_oauth_credentials` |
| `OAUTH_USERS` | Take refresh lock | `slack_user_id` | no lock, lock expired, or we own it | `DynamoHelper.refresh_access_token` |
| `OAUTH_USERS` | Store refreshed tokens | `slack_user_id` | `refreshLockOwner = me`, removes the lock | `DynamoHelper.refresh_access_token` |
| `APPROVALS` | Create approval + dedupe row | `approval_id` ×2 | **Transaction**: new approval id, and no live dedupe row (`attribute_not_exists OR expires_at <= now`) | `ApprovalRepository.create_pending` |
| `APPROVALS` | Claim / reject | `approval_id` | `status = PENDING AND slack_user_id = caller AND expires_at > now` | `ApprovalRepository.claim`, `reject` |
| `APPROVALS` | Cancel undelivered | `approval_id` | `status = PENDING` | `ApprovalRepository.cancel` |
| `APPROVALS` | Finish | `approval_id` | `status = EXECUTING` | `ApprovalRepository.finish` |
| `APPROVALS` | Release dedupe row | `dedupe#…` | `target_approval_id = this approval` (never frees a newer request's slot) | `ApprovalRepository.release_dedupe` |

### Retention

| Item | Removed by | When |
|---|---|---|
| OAuth state | TTL on `expiresAt` | 10 min after creation (single use even before that) |
| Pending approval | TTL on `ttl` | 24 h after creation if never handled (the Approve button stops working at `expires_at`) |
| Handled approval | TTL on `ttl` | `APPROVAL_RETENTION_DAYS` (default 90) after the last status change – audit trail |
| Dedupe row | TTL on `ttl`, or deleted on reject / cancel / failure | 24 h after creation |
| Jira connection | Never (no disconnect yet – see spec gap GAP-04) | – |

DynamoDB TTL deletes lazily (usually within 48 h), so every expiry is also enforced in a condition
(`expires_at > now`, `expiresAt > now`) instead of relying on the row being gone.

## 2. Approval state machine

```mermaid
stateDiagram-v2
    [*] --> PENDING : create_pending (with dedupe row)
    PENDING --> EXECUTING : claim by owner before expires_at
    PENDING --> REJECTED : reject by owner before expires_at
    PENDING --> CANCELLED : approval message could not be posted
    PENDING --> Expired : expires_at passed, buttons refuse
    EXECUTING --> APPROVED_EXECUTED : Jira worklog created
    EXECUTING --> EXECUTION_FAILED : Jira error or bad action
    EXECUTING --> CANCELLED : Jira site changed since the request (SC-01)
    APPROVED_EXECUTED --> [*]
    EXECUTION_FAILED --> [*]
    REJECTED --> [*]
    CANCELLED --> [*]
    Expired --> [*] : TTL deletes the row
```

- Only `PENDING → EXECUTING` leads to a Jira write, and only `handle_approval_action` performs it.
- Site binding (SC-01): `jira_cloud_id` is stored at creation. After the claim, it is compared with the cloud
  id of the credentials that will perform the write; on a mismatch (user reconnected to another site) the
  approval ends `CANCELLED` and nothing is written. Rows created before SC-01 have no `jira_cloud_id` and are
  not checked.
- The dedupe row is released on `REJECTED`, `CANCELLED` and `EXECUTION_FAILED`, so the user can resubmit.
  After `APPROVED_EXECUTED` it keeps blocking an identical worklog for 24 h.
- `Expired` is not stored: the row stays `PENDING` until TTL removes it, and the time check in the condition
  refuses any click.
- Known gap (TC-29 / GAP-07): if Jira accepted the worklog but writing `APPROVED_EXECUTED` fails, the row ends
  `EXECUTION_FAILED` even though the worklog exists. In that case the dedupe row is **kept**, so the same
  worklog cannot be resubmitted for 24 h. The user is told to check Jira before retrying.
- A Slack timeout while posting the approval leaves it `PENDING` (Slack may have posted it). Only a definite
  Slack error (`ok: false` or an HTTP error) cancels it.

## 3. Layer structure

```mermaid
classDiagram
    direction LR
    class lambda_handler {
        +route(event) response
    }
    class SlackCommandHandler {
        +handle_slack_request()
    }
    class SlackActionHandler {
        +handle_block_actions()
        +handle_view_submission()
    }
    class AsyncTaskHandler {
        +handle_async_task(event)
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
    }
    lambda_handler --> SlackCommandHandler : signed Slack request
    lambda_handler --> AsyncTaskHandler : async event + secret
    SlackCommandHandler --> SlackActionHandler : interactivity payload
    AsyncTaskHandler --> AgentOrchestrator : agent_request
    AsyncTaskHandler --> ApprovalService : approval tasks
    AgentOrchestrator --> BedrockLLMClient : plan only
    AgentOrchestrator --> ToolRegistry : validated plan
    ToolRegistry --> ApprovalService : prepare_worklog
    ToolRegistry --> JiraService : read intents
    ApprovalService --> ApprovalRepository
    ApprovalService --> JiraService : write after claim
    JiraService --> JiraClient
    JiraClient --> DynamoHelper : tokens
```

The model never reaches `JiraService` directly: `ToolRegistry` validates the plan against `ALLOWED_INTENTS`,
and the only write it can trigger is creating a PENDING approval.

## 4. Sequence diagrams

### 4.1 Log Time modal → approval request (GAP-01, GAP-02, GAP-08)

```mermaid
sequenceDiagram
    autonumber
    actor U as Slack user
    participant SL as Slack
    participant L as Lambda (sync)
    participant A as Lambda (async)
    participant S as ApprovalService
    participant DB as APPROVALS table

    U->>SL: Submit Log Time modal
    SL->>L: POST / view_submission (signed)
    L->>L: Verify signature, validate form (issue key, duration > 0 and <= 24h, estimate)
    alt Validation fails
        L-->>SL: 200 response_action errors (modal stays open)
    else Valid
        L->>A: invoke Event request_worklog_approval (task secret)
        L-->>SL: 200 response_action clear (within 3 s)
        A->>S: request_worklog_approval(user, channel, form)
        rect rgb(240, 249, 255)
            Note over S,DB: Transaction: approval row (PENDING) + dedupe row
            S->>DB: TransactWriteItems
        end
        alt Identical request pending or approved in last 24 h
            DB-->>S: TransactionCanceled (dedupe condition)
            S-->>A: DuplicateApprovalError
            A->>SL: DM user "identical request already exists"
        else Created
            DB-->>S: OK
            S->>SL: chat.postMessage approval with Approve / Reject buttons
            alt Slack rejects (bot not in channel, etc.)
                SL-->>S: error
                rect rgb(254, 242, 242)
                    Note over S,DB: Compensation: PENDING to CANCELLED, release dedupe row
                    S->>DB: UpdateItem status CANCELLED, DeleteItem dedupe row
                end
                S-->>A: ApprovalDeliveryError
                A->>SL: DM user "could not post approval, nothing logged"
            else Posted
                SL-->>U: Approval message in channel
            end
        end
    end
```

### 4.2 Approve → Jira worklog (the only write path)

```mermaid
sequenceDiagram
    autonumber
    actor U as Clicking user
    participant SL as Slack
    participant L as Lambda (sync)
    participant A as Lambda (async)
    participant S as ApprovalService
    participant DB as APPROVALS table
    participant J as Jira REST API

    U->>SL: Click Approve
    SL->>L: POST / block_actions approve_jira_write (signed)
    L->>A: invoke Event handle_approval_action
    L-->>SL: 200 empty (within 3 s)
    A->>S: approve_and_execute(approval_id, user)
    S->>DB: UpdateItem PENDING to EXECUTING if owner and not expired
    alt Not owner, expired or already handled
        DB-->>S: ConditionalCheckFailed
        S-->>A: ok false
        A->>SL: Ephemeral refusal (original message untouched)
    else Claimed
        DB-->>S: approval row
        alt Current Jira site != approval jira_cloud_id
            S->>DB: finish CANCELLED, release dedupe row
            S-->>A: ok false
            A->>SL: Ephemeral "Jira site changed, nothing logged"
        else Same site (or legacy row without jira_cloud_id)
        S->>J: POST issue worklog (timeout 20 s)
        alt Jira success
            J-->>S: 201 worklog
            S->>DB: finish APPROVED_EXECUTED, ttl = now + retention
            S-->>A: ok true
            A->>SL: Ephemeral success + chat.update replaces buttons
        else Jira error or timeout
            J-->>S: 4xx, 5xx or timeout
            rect rgb(254, 242, 242)
                Note over S,DB: finish EXECUTION_FAILED, release dedupe row
                S->>DB: UpdateItem EXECUTION_FAILED, DeleteItem dedupe row
            end
            S-->>A: exception (logged, not retried)
            A->>SL: Ephemeral "check Jira before submitting again"
        end
        end
    end
```

### 4.3 Connect Jira (OAuth 2.0 3LO)

```mermaid
sequenceDiagram
    autonumber
    actor U as Slack user
    participant SL as Slack
    participant L as Lambda
    participant ST as OAUTH_STATES
    participant AT as Atlassian
    participant US as OAUTH_USERS

    U->>SL: /jira-agent connect
    SL->>L: POST / slash command (signed)
    L->>ST: Put state (expiresAt = now + 600)
    L-->>SL: Ephemeral Connect Jira link (authorize URL with state)
    U->>AT: Open link, grant consent
    AT->>L: GET /oauth/callback with code and state
    L->>ST: Consume state (exists, unused, unexpired)
    alt State invalid, expired or reused
        ST-->>L: ConditionalCheckFailed
        L-->>U: 400 HTML "link expired or already used"
    else State valid
        L->>AT: Exchange code for tokens
        L->>AT: accessible-resources, then /myself
        alt No tokens, no site or several sites without JIRA_BASE_URL
            L-->>U: 500 HTML "connection failed" (details only in logs)
        else OK
            L->>US: Put connection (replaces previous site)
            L-->>U: 200 HTML "Jira connected"
        end
    end
```

### 4.4 Natural-language request (agent)

```mermaid
sequenceDiagram
    autonumber
    actor U as Slack user
    participant SL as Slack
    participant L as Lambda (sync)
    participant A as Lambda (async)
    participant O as AgentOrchestrator
    participant B as Bedrock
    participant T as ToolRegistry

    U->>SL: /jira-agent log 1h on PROJ-12 today at 9:00
    SL->>L: POST / slash command (signed)
    L->>A: invoke Event agent_request
    L-->>SL: 200 "I'm processing your Jira request..."
    A->>O: handle(user, text, channel)
    O->>B: Converse (system prompt + date context)
    B-->>O: JSON plan only
    O->>T: execute_plan(plan)
    alt Intent not allow-listed or invalid parameters
        T-->>O: ValueError or clarify message
    else Read intent
        T->>T: JiraService read (list, today, issue worklogs)
    else prepare_worklog
        T->>T: Validate form, ApprovalService.request_worklog_approval (flow 4.1 from the transaction on)
    end
    O-->>A: AgentResult message
    A->>SL: POST response_url (ephemeral reply)
```

## 5. Processing steps – worklog from request to Jira

| Step | Layer | Action | Exception / branch | Result & compensation |
|:-:|---|---|---|---|
| 1 | `lambda_handler` | Verify Slack signature and timestamp (±300 s) | Bad or missing signature | `401 invalid Slack signature` |
| 2 | `SlackActionHandler` / `ToolRegistry` | Validate form: issue key pattern, date, start time, `0 < duration <= MAX_WORKLOG_MINUTES`, estimate option | Invalid field | Modal: `errors` per block. Agent: clarify message. Nothing stored |
| 3 | `SlackActionHandler` | Invoke async task, ACK Slack | Invoke throttled | `500` to Slack (`dispatch_failed`), see GAP-14 |
| 4 | `ApprovalRepository` (transaction) | Create PENDING approval and dedupe row | Live dedupe row exists | `DuplicateApprovalError` → user notified, nothing stored |
| 5 | `ApprovalService` | Post Approve/Reject message | Slack error | Cancel approval and release dedupe row → `ApprovalDeliveryError` → user notified |
| 6 | `ApprovalRepository` | Owner clicks Approve: conditional claim to EXECUTING | Not owner, expired, already handled | Ephemeral refusal, row unchanged |
| 6b | `ApprovalService` | Compare `jira_cloud_id` with the site of the write credentials | Site changed (reconnect) | `CANCELLED`, dedupe released, user told nothing was logged (SC-01) |
| 7 | `JiraService` | POST worklog | Jira error or timeout | `EXECUTION_FAILED`, dedupe released, user told to check Jira. Not retried by Lambda |
| 8 | `ApprovalRepository` | Finish `APPROVED_EXECUTED`, extend `ttl` to retention | DynamoDB error after Jira success | Known gap TC-29: row ends `EXECUTION_FAILED` |
| 9 | `AsyncTaskHandler` | Notify user, replace buttons on success | Slack error | Logged only, best effort |
