---
name: jira-agent-guardrails
description: >-
  Check the jira_agent repo against its CLAUDE.md non-negotiable rules (single approval write path,
  no model-chosen dispatch, no token/description logging, Slack signature + async secret, owner-bound
  claim, DynamoDB resource wire format, HTTP timeouts, stdlib+boto3 only, secrets via SSM). Use before committing or after
  changing approval/, agent/, handlers/, jira/, slack/ or util/. Triggers: guardrails, rule check,
  ガードレール, 規約チェック, kiểm tra quy tắc.
---

# jira-agent guardrails

## 1. Objective
Block any change that breaks a CLAUDE.md non-negotiable rule, with exact `file:line` evidence.

## 2. Rules
- Run the script first; never judge the rules by reading the whole repo. It parses with `ast`, imports nothing.
- Rule details, rationale and fixes: [resources/guardrail-rules.md](resources/guardrail-rules.md). Load only the rows reported.
- Reply in the user's language (en, ja, or vi).

## 3. Steps
1. Run `python "${CLAUDE_SKILL_DIR}/scripts/check_guardrails.py" <repo_root>` (exit 1 = ERROR found).
2. For each finding, read only ±15 lines around `file:line`, confirm it, and give a drop-in fix from the rules file.
3. If the user asked for fixes: apply them, rerun the script and `python -m pytest tests`, and report both results.
4. Never weaken a rule to make the check pass; changing a rule needs the user's explicit OK and a CLAUDE.md update.

## 4. Quality gate
- PASS only when the script exits 0 and the test suite passes.
- BANNED: deleting or skipping the check, adding a write path "temporarily", `print()` for debugging,
  logging tokens/bodies/descriptions, pre-serialized `{"N": ...}` values through `table.meta.client`.
- Manual (why + pitfalls, JA/VI): [docs/manual.html](docs/manual.html)
