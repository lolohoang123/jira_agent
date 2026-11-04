import json
import logging
import os
import urllib.parse
import urllib.request
import urllib.error
import time
from datetime import datetime, timezone

from util.common import json_response, slack_escape
from util.dynamo_helper import DynamoHelper
from util.constant import ATLASSIAN_TOKEN_URL, ATLASSIAN_RESOURCES_URL

logger = logging.getLogger(__name__)


class AuthenticationJira:
    def __init__(self, event):
        self.event = event

    def html_response(self, status_code, title, message):
        html = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>" + slack_escape(title) +
                "</title></head><body style=\"font-family:system-ui,sans-serif;max-width:640px;margin:48px auto;padding:0 20px\"><h2>" +
                slack_escape(title) + "</h2><p>" + slack_escape(message) +
                "</p><p>You can close this tab and return to Slack.</p></body></html>")
        return {"statusCode":status_code,"headers":{"Content-Type":"text/html; charset=utf-8","Cache-Control":"no-store"},"body":html}

    def atlassian_json_request(self, url, method="GET", data=None, access_token=None):
        headers={"Accept":"application/json"}
        body=None
        if data is not None:
            headers["Content-Type"]="application/json"
            body=json.dumps(data).encode()
        if access_token:
            headers["Authorization"]=f"Bearer {access_token}"
        req=urllib.request.Request(url,data=body,headers=headers,method=method)
        try:
            with urllib.request.urlopen(req,timeout=20) as response:
                raw=response.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            logger.warning("Atlassian OAuth/API request failed with HTTP %s",exc.code)
            raise RuntimeError(f"Atlassian OAuth/API request failed with HTTP {exc.code}.") from exc

    def exchange_oauth_code(self, code):
        return self.atlassian_json_request(ATLASSIAN_TOKEN_URL,method="POST",data={
            "grant_type":"authorization_code","client_id":os.environ["ATLASSIAN_CLIENT_ID"],
            "client_secret":os.environ["ATLASSIAN_CLIENT_SECRET"],"code":code,
            "redirect_uri":os.environ["ATLASSIAN_REDIRECT_URI"]})

    def choose_jira_resource(self, resources):
        candidates=[x for x in resources if x.get("id") and x.get("url")]
        preferred=os.environ.get("JIRA_BASE_URL","").rstrip("/").lower()
        if preferred:
            matches=[x for x in candidates if x["url"].rstrip("/").lower()==preferred]
            if len(matches)==1:
                return matches[0]
        if len(candidates)==1:
            return candidates[0]
        if not candidates:
            raise RuntimeError("No Jira site was returned. Check that you granted access to a Jira site.")
        raise RuntimeError("Multiple Jira sites are available. Set JIRA_BASE_URL to the intended site URL, then reconnect.")

    def finish_oauth_callback(self):
        params=self.event.get("queryStringParameters") or {}
        if params.get("error"):
            return self.html_response(400,"Jira connection cancelled","Atlassian did not authorize the connection. Run `/jira-agent connect` again.")
        code,state=params.get("code"),params.get("state")
        if not code or not state:
            return self.html_response(400,"Could not connect Jira","Missing authorization code or state. Run `/jira-agent connect` again.")
        try:
            slack_user_id=DynamoHelper("OAUTH_STATES_TABLE").consume_oauth_state(state)
            token=self.exchange_oauth_code(code)
            access,refresh=token.get("access_token"),token.get("refresh_token")
            if not access or not refresh:
                raise RuntimeError("Atlassian did not return access and refresh tokens. Check offline_access scope.")
            resources=self.atlassian_json_request(ATLASSIAN_RESOURCES_URL,access_token=access)
            site=self.choose_jira_resource(resources if isinstance(resources,list) else [])
            api=f"https://api.atlassian.com/ex/jira/{urllib.parse.quote(site['id'],safe='')}/rest/api/3/myself"
            myself=self.atlassian_json_request(api,access_token=access)
            now=int(time.time()); expires=now+int(token.get("expires_in",3600))
            record={"slack_user_id":slack_user_id,"jiraAccountId":myself.get("accountId",""),
                    "jiraCloudId":site["id"],"jiraSiteUrl":site["url"].rstrip("/"),
                    "jiraSiteName":site.get("name",""),"accessToken":access,"refreshToken":refresh,
                    "accessTokenExpiresAt":expires,
                    "accessTokenExpiresAtIso":datetime.fromtimestamp(expires,tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "scope":token.get("scope",""),"connected":True,"connectedAt":now,"updatedAt":now}
            DynamoHelper("OAUTH_USERS_TABLE").oauth_table().put_item(Item=record)
            logger.info("Stored Jira OAuth connection for Slack user %s",slack_user_id)
            return self.html_response(200,"Jira connected successfully",f"Connected to {site.get('name') or site['url']}. Return to Slack and run `/jira-agent list` or `/jira-agent logwork today`.")
        except ValueError as exc:
            return self.html_response(400,"Could not connect Jira",str(exc))
        except Exception:
            logger.exception("Failed to complete Atlassian OAuth callback")
            return self.html_response(500,"Could not connect Jira","Connection failed. Check Lambda logs and OAuth configuration, then try again.")

    def build_connect_response(self, slack_user_id):
        try:
            url=DynamoHelper("OAUTH_STATES_TABLE").create_oauth_authorization_url(slack_user_id)
            return json_response(200,{"response_type":"ephemeral","text":f"Connect your Jira account: <{url}|*Connect Jira*>. This link expires in 10 minutes and can only be used once."})
        except Exception:
            logger.exception("Could not create Jira OAuth authorization URL")
            return json_response(200,{"response_type":"ephemeral","text":"Could not start Jira connection. Check OAuth and DynamoDB configuration."})
