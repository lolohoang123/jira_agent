# CLAUDE.md

Guidance for Claude when working in this repository.

## What this is
A Slack bot (`/jira-agent`) that lets users connect Jira via Atlassian OAuth 2.0 (3LO), list assigned tickets, view today's worklogs, and log time. Natural-language requests are planned by Amazon Bedrock (Converse API) and executed only through an allow-list. Runs as a single AWS Lambda behind API Gateway, with DynamoDB for state. See `README.md` for deploy and infra details, `docs/BEDROCK_SETUP.md` for Bedrock.

## Layout
- `functions/read_jira_lambda.py` – Lambda entry point (`lambda_handler`); routes Slack commands, interactivity, OAuth callback and async self-invocations.
- `handlers/` – Slack command/action handlers, OAuth handler, async task router.
- `agent/` – Bedrock client (`llm_client.py`), prompt (`instructions.py`), orchestrator, and `tool_registry.py` (allow-listed intents + input validation).
- `jira/` – OAuth exchange, REST transport (`jira_client.py`), business logic (`jira_service.py`), async tasks in `jira/tasks/`.
- `slack/` – Slack Web API client, request signature verification, modal and message block builders.
- `approval/` – approval repository (DynamoDB) and service.
- `util/` – DynamoDB helper, constants, common helpers (e.g. `validate_duration`), async Lambda invoke.
- `tests/` – unit tests (mix of `unittest` and `pytest` styles).
- `_backup_phase1/`, `_backup_phase2/` – old snapshots. **Do not edit or import from these**; ignore them when searching.

## Commands
- Install: `pip install -r requirements.txt pytest` (runtime dependency is only `boto3`).
- Tests: `python -m pytest tests` from the repo root (pytest runs both the unittest and pytest-style tests).
- Config: copy `.env.example` to `.env` for local values; never commit `.env`.
- Guardrails: `python .claude/skills/jira-agent-guardrails/scripts/check_guardrails.py .` (also run in CI).
- Deploy: `template.yaml` (SAM) + `.github/workflows/deploy.yml`; setup steps in `docs/deploy/RELEASE_CHECKLIST.md`.

## Non-negotiable rules
- **The model never calls Jira.** Bedrock only returns a JSON plan; `agent/tool_registry.py` validates it and executes only intents in `ALLOWED_INTENTS`. Never dispatch functions by a model-supplied name. Adding a capability means adding an explicit intent plus validation there.
- **All worklog writes go through `ApprovalService`.** The modal and the agent create a PENDING approval; only the async task `handle_approval_action` may write to Jira, after a conditional `PENDING → EXECUTING` claim. Do not call `create_jira_worklog` directly or add any other write path.
- Only the Slack user who created an approval may approve/reject it.
- Never log OAuth tokens, Slack request bodies, or worklog descriptions.
- Read secrets only via `util/config.get_secret` (SSM Parameter Store in AWS, env vars locally); never `os.environ` for them.
- Verify Slack signatures (`slack/slack_security.py`) on every Slack request; async self-invocations are authenticated with `ASYNC_TASK_SECRET`.
- Slack must get an ACK within 3 seconds: slow work (Bedrock, Jira calls) runs via async self-invoke (`util/lambda_invoke.py`).

## Conventions
- Python 3.12 (Lambda runtime). Keep dependencies minimal – stdlib + `boto3`; HTTP calls use the standard library.
- Timezone is per user (`util/timezone.py`): `timeZone` on the Jira connection, taken from Jira `/myself` on connect (Slack `users.info` `tz` as fallback; backfilled from Jira for older rows). `JIRA_TIMEZONE` (default `Asia/Ho_Chi_Minh`) is only the fallback. Never read `JIRA_TIMEZONE` directly for "today" or worklog start times.
- Token expiry is stored as Unix seconds; reads must stay backward-compatible with old millisecond values.
- DynamoDB TTL attributes: `ttl` on `APPROVALS_TABLE`, `expiresAt` on `OAUTH_STATES_TABLE`.
- Build Jira issue links from the user's stored `jiraSiteUrl`, not a global URL.
- Multi-workspace: every Slack identity is (`team_id`, `slack_user_id`) – Jira connections, approvals (owner check), dedupe keys, rate limits and OAuth states all carry `team_id`. Call Slack with `slack_api(method, payload, team_id)` (bot token from the installations table); never assume one global bot token.
- One Jira site per Slack user (per workspace); reconnecting replaces the previous connection.
- Inject clients (Bedrock, DynamoDB, Jira) so tests can use fakes/mocks; tests must not hit real AWS, Slack or Jira.
