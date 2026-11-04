import logging
from jira.tasks.run_create_worklog_task import RunCreateWorklogsTask
from jira.tasks.run_today_worklogs_task import RunTodayWorklogsTask
from jira.tasks.run_list_assigned_task import RunListAssignedTask
from util.common import json_response

logger=logging.getLogger(__name__)


def handle_async_task(event):
    task=event.get("_async_task")
    if task=="create_jira_worklog":
        RunCreateWorklogsTask(event,event.get("slack_user_id")).run_create_worklog_task()
    elif task=="jira_worklog":
        RunTodayWorklogsTask(event).run_today_worklogs_task()
    elif task=="list_assigned_issues":
        RunListAssignedTask(event).run_list_assigned_task()
    else:
        return json_response(400,{"error":"unknown task"})
    return json_response(200,{"ok":True})
