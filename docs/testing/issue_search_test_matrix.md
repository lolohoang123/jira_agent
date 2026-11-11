# Test Matrix – `/jira-agent logwork find <keyword>`

Search Jira tickets by keyword, show total logged time per ticket, and log work through the existing
Log Time modal and approval flow. Covers `handlers/slack_command_handler.py` (parsing, dispatch),
`jira/jira_service.py` (`jql_text_terms`, `search_issues`), `slack/message_builder.py`
(`build_search_result_blocks`), `jira/tasks/run_search_issues_task.py` and task routing.
Jira, Slack, DynamoDB and Lambda are mocked. Tests: `tests/test_issue_search.py`.

| ID | Category | Input | Expected |
|----|----------|-------|----------|
| TC-01 | Normal | `logwork find login bug`, `find PROJ-12`, upper case, curly or straight quotes | keyword extracted, original case kept, quotes removed |
| TC-02 | Boundary | `logwork find` with no keyword; `logwork findx`, `finder x`, `logwork today`, `list` | "" (usage) / not a search command |
| TC-03 | Normal | valid search command | async task `search_issues` with keyword and task secret; reply "Searching Jira tickets..." |
| TC-04 | Abnormal | keyword empty or only special characters | usage message; no Jira call, no invoke |
| TC-05 | Abnormal | user not connected | "not connected" message; no invoke |
| TC-06 | Boundary | keyword longer than 100 characters | keyword sent to the task is cut to 100 |
| TC-07 | Normal | search while the assistant is disabled | search still works (not routed to the agent) |
| TC-08 | Abnormal | keyword with `"`, `\`, `'`, operators (`OR project = X`, `(*)`, `=`) | only plain search terms remain; JQL structure cannot change |
| TC-09 | Normal | keyword `login bug` | one search: `text ~ "login*" AND text ~ "bug*" ORDER BY updated DESC`, fields summary/status/timespent, maxResults 21, one page |
| TC-09b | Normal / Boundary | LIKE clause: `logi`, `logi bu`, Vietnamese, one-character words, more than 5 words, user `*` / operators, empty | prefix match per word joined by AND; one-character words exact; at most 5 words; only our own `*`; "" when nothing usable |
| TC-10 | Normal | keyword is an issue key (`PROJ-12`, `proj-12`) | issue fetched directly and listed first, not duplicated |
| TC-11 | Abnormal | issue key not found / no permission | direct fetch ignored; text results returned |
| TC-12 | Boundary | 25 matches | at most 21 returned (20 shown + "more" marker) |
| TC-13 | Normal | result blocks | header with escaped keyword, link to the user's site, status, total logged time, Log Time button valued with the key |
| TC-14 | Boundary | no match / more than 20 / issue without summary or time | "No Jira tickets match", "More than 20 tickets match", "(No summary)", "0m" |
| TC-15 | Normal | `list` output after the shared builder refactor | unchanged text and buttons |
| TC-16 | Normal / Exception | task succeeds / Jira fails | results replace the "Searching" message / error message; keyword not logged |
| TC-17 | Normal | async event `search_issues` | routed to the search task |
