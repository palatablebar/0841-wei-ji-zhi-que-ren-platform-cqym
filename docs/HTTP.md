# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

## 危急值通知闭环

判定为 `CRITICAL_VALUE`（结论 `REJECT`）的每行危急值对应一条通知/待回执任务，以 `specimen_id`（critical_key）幂等。

- `POST /api/runs/{id}/notifications`（别名 `/notify`、`/critical-values`）：为该批次每条危急值幂等建通知，提交 `doctor`/`receiver`、`notified_at`、`due_at` 或 `deadline_minutes`。
- `POST /api/notifications`：直接建/同步单条或多条通知（可带 `run_id`、`profile`、`critical_values` 或单个 `specimen_id`）。
- `GET /api/notifications`（别名 `/api/tasks`、`/api/critical-values`、`/api/notifications/pending`）：列表，返回 `notifications/items/tasks/pending/closed` 与计数。
- `GET /api/notifications/{key}`：单条（key 可为 id、critical_key 或 specimen_id）。
- `PATCH|PUT /api/notifications/{key}`：按请求体推断回执或交班。
- `POST /api/notifications/{key}/receipt`（别名 `/ack`、`/acknowledge`、`/readback`、`/close`，以及 `POST /api/receipts`）：提交 `readback` 读回内容；读回为空返回 `READBACK_REQUIRED(400)` 且不闭环，非空才置 `CLOSED`。
- `POST /api/notifications/{key}/handover`（别名 `/handoff`，或批量 `POST /api/handover`、`/api/handovers`、`/api/shifts/handover`）：提交新当班医师（`doctor`/`on_duty_doctor`/`new_doctor`）与可选 `at`/`now`。未回执任务更换 `owner`；已过 `due_at` 者追加一条交班升级，未到期者只换人。`first_notified_at`、`due_at` 与既有 `escalations` 不变。
- `POST /api/notifications/{key}/escalate`、`GET /api/notifications/{key}/escalations`：手动升级与升级记录。

所有时间字段可传 ISO-8601（含 `Z`）或 `YYYY-MM-DD HH:MM:SS`；调用方显式传入的时间串会原样回显，便于确定性测试。重复通知走“存在即返回”，不新增任务、不删除既有回执与升级。
