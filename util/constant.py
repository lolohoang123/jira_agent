"""Fixed URLs and OAuth scopes. Scope lists must match the Atlassian and Slack app configurations."""

# Atlassian OAuth 2.0 (3LO) scopes requested on /jira-agent connect; offline_access gives a refresh token.
OAUTH_SCOPES = [
    "read:jira-user",
    "read:jira-work",
    "write:jira-work",
    "offline_access",
]
# Atlassian endpoints: consent screen, code/refresh-token exchange, and the list of sites a token can access.
ATLASSIAN_AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ATLASSIAN_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

APPROVALS_TABLE_ENV = "APPROVALS_TABLE"

# Slack OAuth v2 ("Add to Slack"). Bot scopes only: slash command + messages/DMs from the bot.
SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
# users:read -> users.info "tz" (timezone fallback when Jira does not return one)
SLACK_BOT_SCOPES = ["commands", "chat:write", "users:read"]
