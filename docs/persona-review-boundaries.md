# 角色决策与复盘边界

人设模式的 `participation.should_reply` 是角色决定及门禁结束后的准入结果，不能反推规则阈值是否正确。`participation.level/score` 保留规则证据。

新记录在原有 trace schema 3 上增加两个可选块；旧记录没有这些字段时表示未知，不补造历史。

## 阶段事实

`decision_stages` 的 `schema_version` 为 1：

- `rule.level/score`：门禁前的规则证据。
- `persona.action/state/length/reason_code`：角色模型最初提出的动作，保留被作息或频率门禁否决前的版本。`decision_timeout` 和 `decision_invalid_or_failed` 是技术回退，不是角色偏好。
- `gate.evaluated/allowed/reason_code/length_hint/rhythm`：本次门禁事实。未执行时 `allowed` 为 null；异常或频率限制抑制时为 false。`rhythm.state/action` 只取本次门禁，不读取前一轮残留状态。
- `outcome`：继续使用独立执行记录，区分 suppressed、generation_failed、delivery_failed、delivered。角色主动不参与记录为 `stage=persona`，门禁抑制为 `stage=gate`。成功发出任一片段即 delivered，后续失败不能抹去发送事实。影子模式不写实际执行结果。

门禁结果、角色模型动作和最终结果不能提前提供给盲评模型；它们只用于事后解释差异。

## 决策前上下文

`review_context` 的 `schema_version` 为 1，包含 `decision_mode=persona_model`、`persona_fingerprint`、`presence_knob`、`interaction_state` 和 `context_truncated`。这里的交互状态取决策之前的状态。指纹是宿主已有的快照身份，包含会话身份，不能当成跨会话统一的人设行为版本。

此块不保存完整人设、正文、关系猜测或人工答案；也不能替代角色参与原则。缺少有效角色原则或必要前文时，评审应弃权。未建立新的关系、作息或质量评分系统。

## 生成约束

人设生成请求现在附带本次 `delivery_constraints.length_hint/rhythm_state/rhythm_action`，将短醒与收束语义传给生成端。现有短回复截断、发送配额和成功发送后提交状态保持生效。戳一戳按线上信号处理，不强制说话、俏皮语气、身体接触或亲密关系。

自动回归覆盖阶段区分、发送记录及生成请求约束；它不能证明真实模型每次都遵守角色、正确识别熟人或自然表现困倦。这些仍需真实群聊场景验收。
