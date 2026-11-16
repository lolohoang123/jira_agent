"""GET /oauth/callback: Atlassian OAuth 2.0 (3LO) redirect after the user approves Jira access."""
from jira.authentication_jira import AuthenticationJira


def handle_oauth_callback(event):
    """Finish the Jira connection (state check, token exchange, site lookup) and return an HTML page."""
    return AuthenticationJira(event).finish_oauth_callback()
