# Jira Slack Agent – modular MVP

## Chức năng hiện có
- `/jira-agent connect`: Atlassian OAuth 2.0 (3LO), lưu connection theo `slack_user_id`.
- `/jira-agent list`: ticket đang assign cho Jira account của Slack user, tổng thời gian worklog theo ticket và nút Log Time.
- `/jira-agent logwork today`: worklog trong ngày theo timezone cấu hình, có tổng thời gian và nút Log Time.
- Modal Log Time: validate form và gọi Lambda async để tạo worklog.
- Refresh access token và lưu refresh token mới; expiry lưu Unix seconds, đọc tương thích record cũ milliseconds.

## Cấu trúc
```text
functions/read_jira_lambda.py       # Lambda entry point
handlers/                           # Slack commands, actions, OAuth, async routing
agent/                               # Agent scaffold; LLM chưa bật
jira/authentication_jira.py          # OAuth callback and Atlassian OAuth exchange
jira/jira_client.py                  # Jira REST transport
jira/jira_service.py                 # Jira business logic
jira/tasks/                          # Async task handlers
slack/                               # Slack API, security, modal, message blocks
approval/                            # Approval boundary scaffold
util/                                # DynamoDB, constants, common helpers
tests/
```

## Deploy
1. Đặt Lambda handler thành `functions.read_jira_lambda.lambda_handler`.
2. Upload toàn bộ source tree (hoặc ZIP) cùng `requirements.txt`/dependency layer có `boto3`.
3. Cấu hình các biến trong `.env.example` trên Lambda.
4. Tạo DynamoDB table `OAUTH_STATES_TABLE` partition key string `state`; table `OAUTH_USERS_TABLE` partition key string `slack_user_id`.
5. Slack slash command request URL trỏ Function URL; OAuth redirect URI phải khớp chính xác `/oauth/callback`.
6. Lambda execution role cần quyền `dynamodb:GetItem`, `PutItem`, `UpdateItem` trên hai bảng và `lambda:InvokeFunction` trên chính function.
7. Slack app cần `commands`, `chat:write`, `chat:write.public` nếu cần, `views:write`; subscribe interactivity request URL cùng Function URL.

## Lưu ý trước production
- Agent/LLM chưa được kết nối; các slash command vẫn chạy deterministic như hiện tại.
- Approval module mới là interface scaffold; modal worklog hiện tại submit là hành động ghi trực tiếp theo behavior cũ. Nếu muốn mọi write phải approval trước, cần thêm bước confirm UI và DynamoDB approval table.
- Refresh lock giảm concurrent refresh; vẫn nên test với DynamoDB thật và Lambda retry. Không log OAuth token, Slack body hoặc mô tả worklog.
- MVP hiện lưu một Jira site cho mỗi Slack user; reconnect site khác sẽ thay connection cũ.
- Jira issue link lấy từ `jiraSiteUrl` theo từng Slack user, không dùng global URL nếu đã có trong OAuth record.
- Nên chạy unit/integration test với Jira sandbox trước khi deploy production.
