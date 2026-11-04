from jira.authentication_jira import AuthenticationJira


def handle_oauth_callback(event):
    return AuthenticationJira(event).finish_oauth_callback()
