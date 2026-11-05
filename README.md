# Jira Slack Agent – modular MVP

## Chức năng hiện có
- `/jira-agent connect`: Atlassian OAuth 2.0 (3LO), lưu connection theo `slack_user_id`.
- `/jira-agent list`: ticket đang assign cho Jira account của Slack user, tổng thời gian worklog theo ticket và nút Log Time.
- `/jira-agent logwork today`: worklog trong ngày theo timezone cấu hình, có tổng thời gian và nút Log Time.
- Modal Log Time: validate form và tạo yêu cầu approval; chưa ghi Jira ngay.
- Approval: lưu PENDING vào DynamoDB, Slack gửi Approve/Reject; chỉ đúng Slack user tạo yêu cầu mới được approve/reject. Approve dùng conditional update PENDING → EXECUTING để chống click lặp, sau đó mới tạo worklog.
- Bedrock Agent: dùng Converse API để phân tích câu lệnh tự nhiên thành JSON plan; backend chỉ thực thi các intent trong allow-list. Yêu cầu tự nhiên chạy bất đồng bộ để Slack nhận ACK trong thời gian ngắn.
- Refresh access token và lưu refresh token mới; expiry lưu Unix seconds, đọc tương thích record cũ milliseconds.

## Cấu trúc
```text
functions/read_jira_lambda.py       # Lambda entry point
handlers/                           # Slack commands, actions, OAuth, async routing
agent/                               # Bedrock LLM adapter, orchestrator, allow-listed tools
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
4. Tạo DynamoDB tables: `OAUTH_STATES_TABLE` partition key string `state`; `OAUTH_USERS_TABLE` partition key string `slack_user_id`; `APPROVALS_TABLE` partition key string `approval_id`. Bật DynamoDB TTL cho attribute `ttl` trên bảng approvals.
5. Slack Slash Command Request URL và Interactivity Request URL trỏ đến API Gateway endpoint đang tích hợp với Lambda; Atlassian OAuth redirect URI phải khớp chính xác API Gateway URL cộng `/oauth/callback`.
6. Lambda execution role cần quyền DynamoDB trên các bảng, `lambda:InvokeFunction` trên chính function và `bedrock:InvokeModel` cho model/inference profile đã chọn.
7. Thêm `BEDROCK_MODEL_ID` và `BEDROCK_REGION` theo model đã chọn. Xem `docs/BEDROCK_SETUP.md` để setup chi tiết.
8. Slack app cần `commands`, `chat:write`, `chat:write.public` nếu cần, `views:write`; bật Interactivity và dùng API Gateway endpoint.

## Lưu ý trước production
- Bedrock chỉ đề xuất JSON intent/parameters. `agent/tool_registry.py` validate allow-list và dữ liệu; model không được gọi Jira API.
- Tất cả đường ghi worklog từ modal hiện tại đều đi qua Approval Service. Không dùng trực tiếp task `create_jira_worklog` cũ; chỉ async task `handle_approval_action` mới được phép thực thi sau khi claim approval thành công.
- Mọi yêu cầu tạo worklog từ Agent đều qua ApprovalService; model không được gọi Jira write API trực tiếp. Python vẫn validate và enforce approval.
- Refresh lock giảm concurrent refresh; vẫn nên test với DynamoDB thật và Lambda retry. Không log OAuth token, Slack body hoặc mô tả worklog.
- MVP hiện lưu một Jira site cho mỗi Slack user; reconnect site khác sẽ thay connection cũ.
- Jira issue link lấy từ `jiraSiteUrl` theo từng Slack user, không dùng global URL nếu đã có trong OAuth record.
- Nên chạy unit/integration test với Jira sandbox trước khi deploy production.
