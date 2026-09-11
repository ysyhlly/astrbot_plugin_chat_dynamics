# 收件人标注 schema v2

现有 `topic_annotations` GET/POST Web API 已接受收件人纠错字段；回放详情保留话题标注，并提供可展开的收件人与回复纠错表单。可编辑是否对 Bot 说话、是否应回复、判断正误、收件人与讨论对象 ID 和错误类型；保存后显示结果，再次打开会恢复已保存值。ID 以逗号分隔，留空表示未标注，输入 `[]` 表示无对象。原有 JSON 导出包含这些人工字段与决策快照。路由注册在 `/{PLUGIN_NAME}/topic_annotations`，实际宿主 URL 前缀沿用现有插件 Web API。

POST 必须保留原四字段 `session_key`、`msg_id`、`expected_topic`、`error_type`，均为非空字符串（最多 256 字符）。旧客户端请求不需修改。`expected_topic` 继续支持现有话题 ID、`NEW`、`UNKNOWN`、`CORRECT`；话题错误类型仍按原规则验证。

可选字段：

| 字段 | 类型及含义 |
| --- | --- |
| recipient_correct | 布尔值，人工判断收件人推断是否正确 |
| bot_targeted | 布尔值，人工判断是否对 Bot 说话 |
| recipient_ids | 去重字符串数组，人工标注收件人 |
| subject_ids | 去重字符串数组，人工标注被讨论对象 |
| expected_reply | 布尔值，人工判断是否应回复 |
| recipient_error_type | correct / missed_bot / false_bot / wrong_recipient / missing_recipient / subject_confusion / unknown |

ID 数组最多 64 项，每项非空且不超过 256 字符；空数组表示没有对象，省略字段表示未标注。其他字段一律拒绝；布尔字段不接受数字或字符串。

记录新增 `annotation_schema_version: 2`。仍沿用按会话隔离的原 KV 键，每会话最多 2000 条，同消息新标注替换旧标注。仅活 DAG 中存在的消息可提交。已保存记录可经 GET 的 `records` 读取；旧记录可直接读取，不会被计入收件人样本。

GET 保留原 `metrics`，新增独立 `recipient_metrics`：收件人样本总数、错误类型计数、明确标记正确/错误的数量和明确要求回复的数量。未填写的字段不会推断为 false。这些仅是人工选择样本的统计，不代表真实准确率。

提交时深拷贝话题诊断与白名单 `decision_trace`，保存当时收件人、话题、参与决策和版本证据；后续实时状态改变不会改写已标注快照。快照包含诊断所需参与者 ID，正文默认不保存；只有 `console_show_message_content=true` 时才保存最多 2000 字的正文，关闭后 GET 隐去此前正文。标注不会修改实时路由，也不会自动训练或静默采集消息全文。缺少正文的快照用于离线核查决策证据，不能据此声称已具备完整对话重放数据集。
