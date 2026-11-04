import json
import logging
import os
import urllib.parse
from urllib.parse import parse_qs

from handlers.slack_action_handler import handle_block_actions, handle_view_submission
from jira.authentication_jira import AuthenticationJira
from jira.jira_service import JiraService
from jira.tasks.run_today_worklogs_task import RunTodayWorklogsTask
from jira.tasks.run_list_assigned_task import RunListAssignedTask
from slack.slack_client import post_slack_response
from util.common import json_response
from util.dynamo_helper import DynamoHelper

logger=logging.getLogger(__name__)


def handle_slack_request(event, context, raw_body, lambda_client):
    headers={k.lower():v for k,v in (event.get("headers") or {}).items()}
    if "application/x-www-form-urlencoded" in headers.get("content-type",""):
        form=parse_qs(raw_body)
        if "payload" in form:
            payload=json.loads(form["payload"][0])
            if payload.get("type")=="block_actions": return handle_block_actions(payload)
            if payload.get("type")=="view_submission": return handle_view_submission(payload,context,lambda_client)
            return json_response(200,"")

    data=parse_qs(raw_body)
    command=data.get("command",[""])[0]
    text=" ".join(data.get("text",[""])[0].split()).lower()
    slack_user_id=data.get("user_id",[""])[0]
    response_url=data.get("response_url",[""])[0]
    if command!="/jira-agent" or text not in {"connect","logwork today","list"}:
        return json_response(200,{"response_type":"ephemeral","text":"Usage: `/jira-agent connect`, `/jira-agent logwork today`, or `/jira-agent list`"})
    parsed=urllib.parse.urlparse(response_url)
    if parsed.scheme!="https" or parsed.hostname not in {"hooks.slack.com","hooks.slack-gov.com"}:
        return json_response(400,{"error":"invalid response_url"})
    if text=="connect":
        return AuthenticationJira({}).build_connect_response(slack_user_id)
    try:
        account_id=DynamoHelper("OAUTH_USERS_TABLE").get_jira_account_id_for_slack_user(slack_user_id)
    except Exception:
        return json_response(200,{"response_type":"ephemeral","text":"Your Jira account is not connected. Run `/jira-agent connect` first."})
    task_type="jira_worklog" if text=="logwork today" else "list_assigned_issues"
    task={"_async_task":task_type,"_task_secret":os.environ["ASYNC_TASK_SECRET"],
          "slack_user_id":slack_user_id,"account_id":account_id,"response_url":response_url}
    lambda_client.invoke(FunctionName=context.function_name,InvocationType="Event",Payload=json.dumps(task).encode())
    return json_response(200,{"response_type":"ephemeral","text":"Fetching your Jira data..."})
