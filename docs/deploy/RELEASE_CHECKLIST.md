# Release checklist – jira-agent (single Slack workspace, many users)

Order matters: AWS secrets → first deploy → Atlassian app → Slack app → smoke test.
Use **separate** Atlassian and Slack apps for `staging` and `prod` so callback URLs and secrets never mix.

## 1. AWS: secrets in SSM Parameter Store (#6)

The Lambda reads four secrets from `/jira-agent/<stage>/` (SecureString, one `GetParameters` call, cached 5 min).
Nothing secret is stored in `template.yaml`, GitHub, or Lambda environment variables.

| Parameter | Value |
|---|---|
| `/jira-agent/<stage>/slack-signing-secret` | Slack app → Basic Information → Signing Secret |
| `/jira-agent/<stage>/slack-bot-token` | Slack app → OAuth & Permissions → Bot User OAuth Token (`xoxb-…`) |
| `/jira-agent/<stage>/atlassian-client-secret` | Atlassian app → Settings → Secret |
| `/jira-agent/<stage>/async-task-secret` | Random, at least 32 bytes (generate below) |

Create or rotate them (repeat per stage; never paste secrets into tickets or chat):

```bash
STAGE=staging
python -c "import secrets; print(secrets.token_urlsafe(48))"   # value for async-task-secret
aws ssm put-parameter --type SecureString --overwrite --name /jira-agent/$STAGE/async-task-secret --value '<generated>'
aws ssm put-parameter --type SecureString --overwrite --name /jira-agent/$STAGE/slack-signing-secret --value '<from Slack>'
aws ssm put-parameter --type SecureString --overwrite --name /jira-agent/$STAGE/slack-bot-token --value '<xoxb-…>'
aws ssm put-parameter --type SecureString --overwrite --name /jira-agent/$STAGE/atlassian-client-secret --value '<from Atlassian>'
```

- Default encryption key `aws/ssm` needs no extra IAM. With a customer-managed key, pass `SsmKmsKeyArn` to the stack.
- After rotating, running functions pick up the new value within `SECRET_CACHE_SECONDS` (default 300 s).
- Rotating `async-task-secret` makes in-flight async tasks fail with 403 for up to 5 minutes; rotate off-peak.

## 2. GitHub: environments and OIDC deploy role (#7)

### 2a. AWS side (done once per account): `infra/github-oidc-bootstrap.yaml`

Creates the GitHub OIDC provider, `jira-agent-github-deploy-staging` / `-prod` (trusted only for
`repo:lolohoang123/jira_agent:environment:<stage>`), and the permissions boundary `jira-agent-function-boundary`.
The deploy roles can only create IAM roles that carry this boundary, so a compromised workflow cannot create an
administrator role. Deploy with an administrator MFA session (Windows/Git Bash: `export MSYS_NO_PATHCONV=1`):

```bash
aws cloudformation deploy --profile mfa --stack-name jira-agent-github-bootstrap   --template-file infra/github-oidc-bootstrap.yaml --capabilities CAPABILITY_NAMED_IAM
aws cloudformation describe-stacks --profile mfa --stack-name jira-agent-github-bootstrap --query "Stacks[0].Outputs"
```

If the account already has the `token.actions.githubusercontent.com` provider, add
`--parameter-overrides CreateOidcProvider=false`.

### 2b. GitHub side (repository Settings)

Environments with secrets and required reviewers need a **public repository, or GitHub Pro / Team / Enterprise
for a private one**.

1. **Settings → Environments → New environment** `staging`:
   - Environment secret `AWS_DEPLOY_ROLE_ARN` = output `StagingDeployRoleArn`
   - Environment variables `AWS_REGION=ap-southeast-1`, `ATLASSIAN_CLIENT_ID`, `BEDROCK_MODEL_ID`, optional `ALARM_EMAIL`
   - Deployment branches: **Selected branches → `main`**
2. **New environment** `prod`: same secret (output `ProdDeployRoleArn`) and variables (prod values), plus
   **Required reviewers** and **Prevent self-review**; deployment branches `main` only.
3. Commit and push `.github/workflows/`, `infra/`, `template.yaml`, `samconfig.toml` to `main`.
4. CI (`ci.yml`) runs tests, guardrails and `cfn-lint` on every PR. Deploy (`deploy.yml`): every push to `main`
   deploys `staging`; `prod` via **Actions → Deploy → Run workflow → stage: prod**, which waits for a reviewer.

## 3. First deploy

```bash
sam build
sam deploy --config-env staging --parameter-overrides Stage=staging AtlassianClientId=<id> BedrockModelId=<model>
```

Note the outputs `ApiUrl` and `OAuthCallbackUrl`. If `AlarmEmail` is set, confirm the SNS subscription e-mail.
Enable Bedrock model access for `BedrockModelId` in `BedrockRegion`.

## 4. Atlassian app: allow other users to authorize (#2)

In the Atlassian Developer Console (developer.atlassian.com → My apps → your app). Labels can change; check the
console if a step looks different.

- [ ] **Authorization → OAuth 2.0 (3LO) → Callback URL** = the stack output `OAuthCallbackUrl`, exactly.
- [ ] **Permissions → Jira API** scopes = `read:jira-user`, `read:jira-work`, `write:jira-work`
      (plus `offline_access`, requested by the app for refresh tokens). Must match `util/constant.py`.
- [ ] **Distribution → Edit → Sharing**. Without this, only the app owner can complete the consent screen and
      every other user's `/jira-agent connect` fails.
- [ ] Fill in vendor name, **privacy policy URL**, and the **personal data declaration**. The app stores, per Slack
      user: Jira account id, site URL/name, OAuth access and refresh tokens; and worklog requests (issue, date,
      duration, description) for the approval retention period. See section 7.
- [ ] Copy the client id to the GitHub variable `ATLASSIAN_CLIENT_ID` and the secret to SSM.

## 5. Slack app

- [ ] **Slash Commands → `/jira-agent`** Request URL = `ApiUrl`.
- [ ] **Interactivity & Shortcuts** on, Request URL = `ApiUrl`.
- [ ] **OAuth & Permissions → Bot Token Scopes**: `commands`, `chat:write` (and `chat:write.public` only if
      `ApprovalDelivery=channel` must post in channels the bot was not invited to).
- [ ] **App Home → Messages Tab** enabled. Approvals are sent as a DM from the bot (`ApprovalDelivery=dm`,
      default); users who cannot open the bot DM get "couldn't send you the approval" instead of a request.
- [ ] Reinstall the app to the workspace after changing scopes; store the new bot token in SSM.

## 6. Smoke test (staging, then prod)

| # | Action | Expected |
|---|---|---|
| 1 | `/jira-agent connect` → open link → consent | "Jira connected successfully" page |
| 2 | `/jira-agent list` | Your assigned tickets with links to your Jira site |
| 3 | Log Time from a ticket → submit | Approval arrives as a **DM** from the bot; nothing in the channel |
| 4 | A second user looks in the channel | Sees nothing about the worklog (only the owner's DM has it) |
| 5 | Approve | Worklog appears in Jira once; DM buttons replaced by "created successfully" |
| 6 | Submit the identical worklog again | DM: "identical worklog request … already" |
| 7 | 21 free-text requests within an hour | 21st reply: "limit of 20 assistant requests per hour" |
| 8 | `/jira-agent disconnect`, then `/jira-agent list` | "disconnected …", then "not connected" |
| 9 | CloudWatch | No errors; logs contain no tokens or descriptions; alarms in OK state |

## 7. Data retention (GAP-04)

| Data | Where | Kept until |
|---|---|---|
| OAuth state | `jira-agent-<stage>-oauth-states` | 10 minutes (single use) |
| Jira connection and tokens | `jira-agent-<stage>-users` | `/jira-agent disconnect` (row deleted), or Atlassian rejects the refresh token (`invalid_grant`: tokens deleted, row kept as disconnected) |
| Approvals (incl. description) | `jira-agent-<stage>-approvals` | 24 h if never handled; `APPROVAL_RETENTION_DAYS` (default 90) after approve/reject/cancel/failure |
| Rate-limit counters | `jira-agent-<stage>-rate-limits` | 2 hours |
| Failed async payloads | SQS `jira-agent-<stage>-async-failures` | 4 days (encrypted; contains the task secret and the form: restrict access) |
| Logs | CloudWatch `/aws/lambda/jira-agent-<stage>` | `LogRetentionDays` (default 30) |

`/jira-agent disconnect` deletes the stored tokens; it cannot revoke the grant at Atlassian. Users revoke it in
their Atlassian account settings (Connected apps).

## 8. Rollback

- Code: re-run **Deploy** on the previous commit (or `git revert` and push to `main` for staging).
- Tables have `DeletionPolicy: Retain` and point-in-time recovery (users, approvals); deleting the stack keeps data.
- A secret leak: rotate the SSM parameter (and the Slack/Atlassian secret at the source); the cache expires in 5 min.
