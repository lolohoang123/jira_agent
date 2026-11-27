"""Browser routes of the Jira connection flow (no Slack signature; protected by one-time states).

GET /oauth/callback     Atlassian OAuth 2.0 (3LO) redirect after the user approves Jira access.
GET /oauth/select-site  The user picked one of several Jira sites on the site-choice page.
"""
from jira.authentication_jira import AuthenticationJira


def handle_oauth_callback(event):
    """Finish the Jira connection (state check, token exchange, site lookup) and return an HTML page."""
    return AuthenticationJira(event).finish_oauth_callback()


def handle_site_selection(event):
    """Complete a pending connection for the chosen Jira site and return an HTML page."""
    return AuthenticationJira(event).finish_site_selection()
