import urllib.parse
from util.common import slack_escape

# Slack allows at most 50 blocks per message: 1 header + 2 per worklog + 1 context + divider + total.
MAX_WORKLOG_ITEMS=23


def _duration(seconds):
    minutes=max(0,int(seconds))//60; hours,minutes=divmod(minutes,60)
    return f"{hours}h {minutes}m" if hours and minutes else (f"{hours}h" if hours else f"{minutes}m")


def build_today_worklog_blocks(day,worklogs,site_url):
    blocks=[{"type":"section","text":{"type":"mrkdwn","text":f"*Jira Worklogs — {day:%d/%m/%Y}*"}}]
    if not worklogs:
        blocks.append({"type":"section","text":{"type":"mrkdwn","text":"No worklogs found for today."}})
    for item in worklogs[:MAX_WORKLOG_ITEMS]:
        key=item["key"]; link=f"{site_url}/browse/{urllib.parse.quote(key,safe='')}"
        line=f"<{link}|*{slack_escape(key)} {slack_escape(item['summary'])}*>\n🕒 {item['started']:%H:%M} · {_duration(item['seconds'])}"
        if item.get("comment"): line+=f"\n📝 {slack_escape(item['comment'][:250])}"
        blocks.extend([{"type":"section","text":{"type":"mrkdwn","text":line}},
            {"type":"actions","elements":[{"type":"button","action_id":"open_log_time","text":{"type":"plain_text","text":"Log Time"},"style":"primary","value":key}]}])
    if len(worklogs)>MAX_WORKLOG_ITEMS:
        blocks.append({"type":"context","elements":[{"type":"mrkdwn","text":f"_Showing {MAX_WORKLOG_ITEMS} of {len(worklogs)} worklogs._"}]})
    total=sum(x["seconds"] for x in worklogs)
    blocks.extend([{"type":"divider"},{"type":"section","text":{"type":"mrkdwn","text":f"*Total: {_duration(total)}*"}}])
    return blocks


def build_assigned_issue_blocks(issues,site_url):
    blocks=[{"type":"section","text":{"type":"mrkdwn","text":"*Your assigned Jira tickets*\nShowing up to 20 tickets, ordered by recently updated."}}]
    if not issues:
        blocks.append({"type":"section","text":{"type":"mrkdwn","text":"No Jira tickets are currently assigned to you."}})
        return blocks
    for issue in issues[:20]:
        key=issue["key"]; fields=issue.get("fields",{}); summary=fields.get("summary","(No summary)")
        status=(fields.get("status") or {}).get("name","Unknown status")
        seconds=int(fields.get("timespent") or 0)
        link=f"{site_url}/browse/{urllib.parse.quote(key,safe='')}"
        blocks.append({"type":"section","text":{"type":"mrkdwn","text":f"<{link}|*{slack_escape(key)} — {slack_escape(summary)}*>\nStatus: {slack_escape(status)} · Total logged: *{_duration(seconds)}*"},
            "accessory":{"type":"button","action_id":"open_log_time","text":{"type":"plain_text","text":"Log Time"},"style":"primary","value":key}})
    if len(issues)>20:
        blocks.append({"type":"context","elements":[{"type":"mrkdwn","text":f"_Showing 20 of {len(issues)} assigned tickets._"}]})
    return blocks
