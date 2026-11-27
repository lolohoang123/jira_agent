"""Connect a Slack user to Jira with Atlassian OAuth 2.0 (3LO).

Flow:
1. ``/jira-agent connect`` -> build_connect_response: a one-time state row + the Atlassian authorize URL.
2. User approves on Atlassian -> GET /oauth/callback -> finish_oauth_callback:
   consume the state (gives back team_id + slack_user_id), exchange the code for tokens, pick the Jira site,
   read /myself (account id + timezone) and store the connection with tokens encrypted.
3. Only when the user granted access to several Jira sites (and JIRA_BASE_URL does not pick one): the
   tokens are stored as a *pending* connection and the callback shows a page with one link per site.
   GET /oauth/select-site -> finish_site_selection completes the connection for the chosen site.
"""
import html
import json
import logging
import os
import urllib.parse
import urllib.request
import urllib.error
import time
from datetime import datetime, timezone

from util.config import get_secret
from util.common import json_response, slack_escape
from util.dynamo_helper import CONNECTIONS_TABLE, DynamoHelper
from util.field_crypto import seal
from util.constant import ATLASSIAN_TOKEN_URL, ATLASSIAN_RESOURCES_URL
from util.timezone import valid_timezone_name
from slack.slack_client import slack_user_timezone

logger = logging.getLogger(__name__)

SELECT_SITE_PATH = "/oauth/select-site"
_PAGE_STYLE = "font-family:system-ui,sans-serif;max-width:640px;margin:48px auto;padding:0 20px"


def usable_sites(resources):
    """Jira sites from accessible-resources that have both an id and a URL."""
    return [x for x in (resources if isinstance(resources, list) else []) if x.get("id") and x.get("url")]


def select_site_url():
    """Public URL of the site-selection route (same API as the OAuth callback)."""
    callback = os.environ["ATLASSIAN_REDIRECT_URI"].rstrip("/")
    base = callback[:-len("/oauth/callback")] if callback.endswith("/oauth/callback") else callback.rsplit("/", 1)[0]
    return base + SELECT_SITE_PATH


class AuthenticationJira:
    """OAuth helpers; ``event`` is the API Gateway event of the callback ({} when only building a link)."""

    def __init__(self, event):
        self.event = event

    def html_response(self, status_code, title, message):
        """Minimal result page for the user's browser; title and message are HTML-escaped."""
        html_page = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>" + slack_escape(title) +
                     "</title></head><body style=\"" + _PAGE_STYLE + "\"><h2>" +
                     slack_escape(title) + "</h2><p>" + slack_escape(message) +
                     "</p><p>You can close this tab and return to Slack.</p></body></html>")
        return {"statusCode":status_code,"headers":{"Content-Type":"text/html; charset=utf-8","Cache-Control":"no-store"},"body":html_page}

    def site_choice_page(self, state, sites):
        """Page with one link per Jira site; every link carries the same one-time selection state."""
        items = []
        for site in sites:
            href = select_site_url() + "?" + urllib.parse.urlencode({"state": state, "site": site["id"]})
            label = site.get("name") or site["url"]
            items.append(f'<li style="margin:10px 0"><a href="{html.escape(href, quote=True)}"><b>'
                         f'{html.escape(label)}</b></a><br><small>{html.escape(site["url"])}</small></li>')
        body = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Choose your Jira site</title></head>"
                "<body style=\"" + _PAGE_STYLE + "\"><h2>Choose your Jira site</h2>"
                "<p>Your Atlassian account has access to several Jira sites. Pick the one you want to use in Slack:</p>"
                "<ul>" + "".join(items) + "</ul>"
                "<p><small>This page expires in 10 minutes. To switch sites later, run "
                "<code>/jira-agent connect</code> again.</small></p></body></html>")
        return {"statusCode":200,"headers":{"Content-Type":"text/html; charset=utf-8","Cache-Control":"no-store"},"body":body}

    def atlassian_json_request(self, url, method="GET", data=None, access_token=None):
        """JSON request to Atlassian (token endpoint or API). The error body is never logged or returned."""
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
        """Trade the one-time authorization code for an access token and a refresh token."""
        return self.atlassian_json_request(ATLASSIAN_TOKEN_URL,method="POST",data={
            "grant_type":"authorization_code","client_id":os.environ["ATLASSIAN_CLIENT_ID"],
            "client_secret":get_secret("ATLASSIAN_CLIENT_SECRET"),"code":code,
            "redirect_uri":os.environ["ATLASSIAN_REDIRECT_URI"]})

    def choose_jira_resource(self, resources):
        """The Jira site to use: the only one, or the one matching JIRA_BASE_URL (single-company deployments).

        Returns None when several sites remain, so the user picks one (site_choice_page).
        """
        candidates=usable_sites(resources)
        preferred=os.environ.get("JIRA_BASE_URL","").rstrip("/").lower()
        if preferred:
            matches=[x for x in candidates if x["url"].rstrip("/").lower()==preferred]
            if len(matches)==1:
                return matches[0]
        if len(candidates)==1:
            return candidates[0]
        if not candidates:
            raise ValueError("Your Atlassian account did not give access to any Jira site. Run `/jira-agent connect` "
                             "in Slack again and choose a Jira site on the Atlassian page.")
        return None

    def _myself(self, site, access):
        """The user's Jira profile on ``site`` (account id and timezone)."""
        api=f"https://api.atlassian.com/ex/jira/{urllib.parse.quote(site['id'],safe='')}/rest/api/3/myself"
        return self.atlassian_json_request(api,access_token=access)

    def _store_connection(self, team_id, slack_user_id, access, refresh, expires, scope, site, myself):
        """Write the connection row (one per team_id + slack_user_id; reconnecting replaces it)."""
        now=int(time.time())
        ids={"team_id":team_id,"slack_user_id":slack_user_id}
        record={"team_id":team_id,"slack_user_id":slack_user_id,"jiraAccountId":myself.get("accountId",""),
                "jiraCloudId":site["id"],
                # Tokens and site identity are stored encrypted (KMS, bound to this row by the encryption context).
                "jiraSiteUrl":seal(site["url"].rstrip("/"),CONNECTIONS_TABLE,"jiraSiteUrl",**ids),
                "jiraSiteName":seal(site.get("name",""),CONNECTIONS_TABLE,"jiraSiteName",**ids),
                "accessToken":seal(access,CONNECTIONS_TABLE,"accessToken",**ids),
                "refreshToken":seal(refresh,CONNECTIONS_TABLE,"refreshToken",**ids),
                "accessTokenExpiresAt":expires,
                "accessTokenExpiresAtIso":datetime.fromtimestamp(expires,tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "scope":scope,"connected":True,"connectedAt":now,"updatedAt":now}
        # Jira profile timezone first (JQL worklogDate uses it), Slack profile as fallback; neither -> default.
        time_zone=(valid_timezone_name(myself.get("timeZone"))
                   or valid_timezone_name(slack_user_timezone(team_id,slack_user_id)))
        if time_zone:
            record["timeZone"]=time_zone
        DynamoHelper("OAUTH_USERS_TABLE").oauth_table().put_item(Item=record)
        logger.info("Stored Jira OAuth connection for Slack user %s",slack_user_id)
        return self.html_response(200,"Jira connected successfully",f"Connected to {site.get('name') or site['url']}. Return to Slack and run `/jira-agent list` or `/jira-agent logwork today`.")

    def _store_pending(self, team_id, slack_user_id, access, refresh, expires, scope):
        """Several sites: keep the tokens (encrypted) until the user picks one; the row is not usable yet."""
        now=int(time.time())
        ids={"team_id":team_id,"slack_user_id":slack_user_id}
        DynamoHelper("OAUTH_USERS_TABLE").oauth_table().put_item(Item={
            "team_id":team_id,"slack_user_id":slack_user_id,
            "accessToken":seal(access,CONNECTIONS_TABLE,"accessToken",**ids),
            "refreshToken":seal(refresh,CONNECTIONS_TABLE,"refreshToken",**ids),
            "accessTokenExpiresAt":expires,
            "accessTokenExpiresAtIso":datetime.fromtimestamp(expires,tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            # connected=False: every command treats the user as "not connected" until a site is chosen.
            "scope":scope,"connected":False,"pendingSiteSelection":True,"updatedAt":now})

    def finish_oauth_callback(self):
        """Handle GET /oauth/callback and return an HTML page (success, site choice or a readable error)."""
        params=self.event.get("queryStringParameters") or {}
        # The user clicked "Cancel" on the Atlassian consent screen.
        if params.get("error"):
            return self.html_response(400,"Jira connection cancelled","Atlassian did not authorize the connection. Run `/jira-agent connect` again.")
        code,state=params.get("code"),params.get("state")
        if not code or not state:
            return self.html_response(400,"Could not connect Jira","Missing authorization code or state. Run `/jira-agent connect` again.")
        try:
            # 1) One-time, unexpired state -> which Slack user started this flow (CSRF protection).
            team_id,slack_user_id=DynamoHelper("OAUTH_STATES_TABLE").consume_oauth_state(state,kind="jira")
            # 2) Tokens. offline_access is required to get a refresh token.
            token=self.exchange_oauth_code(code)
            access,refresh=token.get("access_token"),token.get("refresh_token")
            if not access or not refresh:
                raise RuntimeError("Atlassian did not return access and refresh tokens. Check offline_access scope.")
            expires=int(time.time())+int(token.get("expires_in",3600))
            scope=token.get("scope","")
            # 3) Which Jira site(s) the user granted access to.
            resources=self.atlassian_json_request(ATLASSIAN_RESOURCES_URL,access_token=access)
            site=self.choose_jira_resource(resources)
            if site is None:
                # Several sites: let the user choose in the browser (one-time link per site).
                self._store_pending(team_id,slack_user_id,access,refresh,expires,scope)
                selection=DynamoHelper("OAUTH_STATES_TABLE").create_site_selection_state(team_id,slack_user_id)
                return self.site_choice_page(selection,usable_sites(resources))
            # 4) The user's Jira account id (used in JQL) and profile timezone, then 5) store the connection.
            return self._store_connection(team_id,slack_user_id,access,refresh,expires,scope,site,self._myself(site,access))
        except ValueError as exc:
            # Expected, user-safe errors (expired/used link, ...): show the message as is.
            return self.html_response(400,"Could not connect Jira",str(exc))
        except Exception:
            # Anything else: generic page; details only in CloudWatch.
            logger.exception("Failed to complete Atlassian OAuth callback")
            return self.html_response(500,"Could not connect Jira","Something went wrong while connecting Jira. Run `/jira-agent connect` in Slack to try again.")

    def finish_site_selection(self):
        """Handle GET /oauth/select-site?state=...&site=<cloudId>: finish a pending connection."""
        params=self.event.get("queryStringParameters") or {}
        state,site_id=params.get("state"),params.get("site")
        if not state or not site_id:
            return self.html_response(400,"Could not connect Jira","This link is incomplete. Run `/jira-agent connect` in Slack again.")
        try:
            # Same one-time protection as the OAuth callback: the link works once, for 10 minutes.
            team_id,slack_user_id=DynamoHelper("OAUTH_STATES_TABLE").consume_oauth_state(state,kind="jira_site")
            pending=DynamoHelper("OAUTH_USERS_TABLE").get_pending_site_selection(team_id,slack_user_id)
            access=pending["access_token"]
            # Re-read the granted sites with the user's token: only one of THEIR sites can be chosen.
            resources=self.atlassian_json_request(ATLASSIAN_RESOURCES_URL,access_token=access)
            site=next((x for x in usable_sites(resources) if x["id"]==site_id),None)
            if site is None:
                raise ValueError("That Jira site is not available for your account. Run `/jira-agent connect` in Slack again.")
            return self._store_connection(team_id,slack_user_id,access,pending["refresh_token"],pending["expires_at"],
                                          pending["scope"],site,self._myself(site,access))
        except ValueError as exc:
            return self.html_response(400,"Could not connect Jira",str(exc))
        except Exception:
            logger.exception("Failed to complete the Jira site selection")
            return self.html_response(500,"Could not connect Jira","Something went wrong while connecting Jira. Run `/jira-agent connect` in Slack to try again.")

    def build_connect_response(self, team_id, slack_user_id):
        """Slack reply (only visible to the user) with a single-use Atlassian authorization link."""
        try:
            url=DynamoHelper("OAUTH_STATES_TABLE").create_oauth_authorization_url(team_id,slack_user_id)
            return json_response(200,{"response_type":"ephemeral","text":f"Connect your Jira account: <{url}|*Connect Jira*>. This link expires in 10 minutes and can only be used once."})
        except Exception:
            logger.exception("Could not create Jira OAuth authorization URL")
            return json_response(200,{"response_type":"ephemeral","text":"I couldn't create a Jira connection link right now. Please run `/jira-agent connect` again in a minute."})
