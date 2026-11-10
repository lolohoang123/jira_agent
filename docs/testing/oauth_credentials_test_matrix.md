# Test Matrix – OAuth connection & Jira credentials

Covers `util/dynamo_helper.py`, `jira/authentication_jira.py` and `jira/jira_client.py`.
Frozen clock: `time.time() == 1_700_000_000`. DynamoDB, Atlassian and Jira HTTP are mocked.
Every test that handles tokens also checks that token values never appear in errors or logs.

## DynamoHelper – tables, account lookup, OAuth state

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-01 | Normal | table env var set | Table(name from env) |
| TC-02 | Boundary | OAUTH_USERS_TABLE unset | legacy fallback table "read-jira-users" |
| TC-03 | Abnormal | OAUTH_STATES_TABLE unset | RuntimeError naming the variable |
| TC-04 | Normal | DYNAMODB_ENDPOINT_URL set | resource with endpoint, AWS_DEFAULT_REGION (default us-east-1), local creds |
| TC-05 | Normal | connected Slack user | jiraAccountId; ConsistentRead, ProjectionExpression |
| TC-06 | Abnormal | empty user id / no row / row without jiraAccountId | ValueError "not connected" (no DynamoDB call for empty id) |
| TC-07 | Normal | create authorization URL | state row (createdAt, expiresAt = now+600, attribute_not_exists); URL has audience, client_id, scopes, redirect_uri, state, response_type=code, prompt=consent |
| TC-08 | Abnormal | create URL for empty user | ValueError; nothing stored |
| TC-09 | Normal | consume valid state | slackUserId; condition exists AND not consumed AND expiresAt > now |
| TC-10 | Abnormal | consume expired/used/unknown state | ValueError "expired or has already been used" |
| TC-11 | Abnormal | consume empty state / row without slackUserId | ValueError |
| TC-12 | Exception | consume with other DynamoDB error | ClientError re-raised |

## DynamoHelper – expiry and credentials

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-13 | Boundary | normalize_expiry None, "", "abc", seconds, 10^12, 10^12+1, milliseconds | 0, 0, 0, same, same, //1000, //1000 |
| TC-14 | Normal | token valid for more than 60 s | credentials returned; no refresh |
| TC-15 | Boundary | token expires in exactly 60 s / 61 s | refresh / no refresh |
| TC-16 | Boundary | legacy millisecond expiry still valid | no refresh (backward compatible) |
| TC-17 | Abnormal | empty user / no row / connected = False | ValueError / RuntimeError / RuntimeError |
| TC-18 | Abnormal | row missing accessToken or jiraCloudId | RuntimeError |
| TC-19 | Normal | expired token | refresh_access_token called; refreshed credentials returned |

## DynamoHelper – token refresh with lock

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-20 | Normal | lock acquired, Atlassian returns new tokens | POST refresh_token grant; store new access + rotated refresh token, expiry, ISO; lock removed in same update (condition owner) |
| TC-21 | Boundary | Atlassian omits refresh_token | old refresh token kept |
| TC-22 | Normal | lock acquired but token already refreshed by another request | returns row; no Atlassian call |
| TC-23 | Normal | lock held by another request, token becomes fresh | waits, returns fresh row; no Atlassian call |
| TC-24 | Abnormal | lock held and token still stale past the 8 s deadline | RuntimeError "please retry"; sleeps between polls |
| TC-25 | Abnormal | row deleted while waiting for the lock | RuntimeError "not found" |
| TC-26 | Exception | lock update fails with a non-conditional error | ClientError re-raised |
| TC-27 | Abnormal | no refresh token stored | RuntimeError "reconnect"; no Atlassian call; lock released |
| TC-28 | Exception | Atlassian HTTP 400 (invalid_grant) | RuntimeError with status only, no chained HTTP body; lock released |
| TC-29 | Exception | Atlassian unreachable / timeout | RuntimeError "Could not reach Atlassian"; lock released |
| TC-30 | Abnormal | Atlassian response without access_token or expires_in | RuntimeError; nothing stored; lock released |
| TC-31 | Exception | lock release itself fails | original error raised |

## AuthenticationJira – OAuth callback

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-32 | Normal | html_response | text/html, Cache-Control no-store, title/message HTML-escaped |
| TC-33 | Normal | one accessible site / sites missing id or url filtered | that site |
| TC-34 | Normal | several sites, JIRA_BASE_URL matches (case, trailing slash) | matching site |
| TC-35 | Abnormal | several sites, no or unmatched JIRA_BASE_URL | RuntimeError "Multiple Jira sites" |
| TC-36 | Abnormal | no sites | RuntimeError "No Jira site" |
| TC-37 | Abnormal | callback with error parameter | 400 "cancelled"; no state consumed |
| TC-38 | Abnormal | callback without code or state | 400; no state consumed |
| TC-39 | Abnormal | state expired or reused | 400 with the state error; no token exchange |
| TC-40 | Normal | valid callback | 200; record stored with account, cloud id, site URL (no trailing slash), tokens, expiry now+expires_in, scope, connected |
| TC-41 | Boundary | token response without expires_in | expiry now+3600 |
| TC-42 | Abnormal | token response without refresh token | 500; nothing stored |
| TC-43 | Exception | Atlassian HTTP error / site choice fails / storage fails | 500 generic page; nothing about tokens in the page or logs |
| TC-44 | Normal | atlassian_json_request | Accept header; JSON body + Content-Type when data; Bearer when token; timeout 20; empty body → {} |
| TC-45 | Exception | atlassian_json_request HTTP error | RuntimeError with status only |
| TC-46 | Normal | exchange_oauth_code | POST authorization_code grant with client id/secret, code, redirect_uri |
| TC-47 | Normal / Exception | build_connect_response ok / DynamoDB fails | ephemeral link (10 minutes, single use) / friendly error |

## JiraClient

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-48 | Normal | client creation | credentials loaded for that Slack user only |
| TC-49 | Boundary | site_url from credentials / trailing slash / fallback JIRA_BASE_URL / none | per-user URL preferred; "" when none |
| TC-50 | Normal | GET with params, path without leading slash, cloud id needing quoting | https://api.atlassian.com/ex/jira/{quoted}/... with encoded params |
| TC-51 | Normal | POST with JSON body | method POST, JSON body, Bearer token, timeout 20 |
| TC-52 | Boundary | empty response body | {} |
| TC-53 | Exception | HTTP 404 with long body | RuntimeError "HTTP 404: <first 300 chars>"; warning log without token |
| TC-54 | Exception | network error | RuntimeError "Unable to connect to Jira API" |
