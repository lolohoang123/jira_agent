# Guardrail rules (G1–G9)

Each rule maps to a CLAUDE.md non-negotiable rule or to a real defect found in this repo.
`ERROR` fails the check (exit 1); `WARN` is reported only.

| Rule | Level | Detects | Why | Fix |
|---|---|---|---|---|
| G1 | ERROR | `create_worklog()` called outside `approval/approval_service.py`; a `create_worklog` defined outside `jira/jira_service.py`; a POST/PUT/DELETE to `/worklog` outside the Jira service | "All worklog writes go through ApprovalService": any other path skips the owner check, expiry and the PENDING→EXECUTING claim, and can write duplicates | Route the write through `ApprovalService.request_worklog_approval` (creates PENDING) and let `handle_approval_action` execute it. Delete unused write tasks |
| G2 | ERROR | `eval`/`exec`/`__import__`/`import_module`; `getattr(x, <non-literal>)` in `agent/`; `globals()[...]`; missing `ALLOWED_INTENTS` | "The model never calls Jira": a model-supplied name must never choose code to run | Add an explicit intent to `ALLOWED_INTENTS` with its own validation branch in `tool_registry.py` |
| G3 | ERROR | `print(...)`; logger calls whose arguments mention token, secret, password, description, raw_body, signature, authorization, credential | "Never log OAuth tokens, Slack request bodies, or worklog descriptions". `print` in Lambda goes straight to CloudWatch (a real leak was removed from `llm_client.py`) | Log ids and counts only: `logger.info("approval %s", approval_id)` |
| G4 | ERROR | Entry point without `verify_slack_signature`, `ASYNC_TASK_SECRET` or `compare_digest`; replay window not exactly `> 300`; an `_async_task` payload dict without `_task_secret` | Unsigned Slack requests or forged async events could approve or write worklogs | Keep the checks in `functions/read_jira_lambda.py`; always build task payloads with `_task_secret` (or via one helper) |
| G5 | ERROR | `approval_repository.py` conditions missing `slack_user_id = :owner`, `team_id = :team`, `#s = :pending`, `expires_at > :now` or `#s = :executing` | "Only the Slack user who created an approval may approve/reject it"; the conditional claim is what makes approve-once and expiry hold under concurrency | Restore the condition expression; never replace it with a read-then-write check |
| G6 | ERROR | `TypeSerializer`/`.serialize()` or one-key typed values (`{"N": ...}`, `{"S": ...}`) in a file that uses `meta.client` | A boto3 *resource* client serializes values itself. Pre-serialized values are wrapped twice: production failed with "Incorrect operand type … operand type: M" on `TransactWriteItems`, and mocked tables did not catch it | Pass plain Python values to `table.meta.client.*`; cover the call with a wire-format test (`tests/test_approval_wire_format.py`) |
| G7 | ERROR | `urlopen(...)` without `timeout=` | Slack needs an ACK in 3 s and async tasks have a Lambda timeout; a hung call blocks both | Add `timeout=` (Slack 8 s, Jira/Atlassian 15–20 s) |
| G8 | ERROR / WARN | ERROR: importing `requests`, `httpx`, `aiohttp`, `urllib3`, or a `_backup_*` module. WARN: runtime dependency other than `boto3` in `requirements.txt` | Runtime dependencies are stdlib + `boto3`; backups are stale snapshots | Use `urllib.request`; put test-only packages in a separate test requirements file |
| G9 | ERROR | `os.environ[...]`, `os.environ.get(...)` or `os.getenv(...)` of `SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN`, `ATLASSIAN_CLIENT_SECRET` or `ASYNC_TASK_SECRET` outside `util/config.py` | Secrets live in SSM Parameter Store (`/jira-agent/<stage>/`); reading the environment would bypass it and invite plaintext secrets in Lambda configuration | `from util.config import get_secret`; `get_secret("SLACK_BOT_TOKEN")` |

## Known limits (static analysis)

- G3 checks names, not values: `logger.info("%s", x)` where `x` holds a token is not detected. Keep sensitive
  values in clearly named variables.
- G1 recognises the write by name (`create_worklog`) and `/worklog` URLs; a renamed write function needs a rule update.
- G4/G5 check that the safeguards are present in the expected files, not that every code path reaches them;
  the unit tests in `tests/test_request_path.py` and `tests/test_approval_matrix.py` cover behaviour.
