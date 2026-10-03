# Jev 决策层（TypeSafe System One）

人设模式下「本轮要不要开口」的决策层后端：把每轮一次的判断交给 TypeSafe 的 System One 决策模型（Jev）回答，再把答案映射回插件自己的 `TurnDecision`。参与门闩、决策轨迹、Shadow 遥测与投递不感知、也无需感知是哪个后端做的决定。

## 职责

| 模块 | 职责 |
| --- | --- |
| `core/jev_decision.py` | 问什么（封闭词表）、答案怎么映射回 `TurnDecision`、置信度门槛在哪 |
| `core/integrations/typesafe.py` | 传输与契约校验；不了解群聊，也不做任何决策 |
| `core/integrations/net_policy.py` | 凭据携带型客户端共用的主机规则（明文密钥不发往非本机） |
| `core/persona_engine.py` | 何时咨询决策层，拿不到答案时如何回落 |
| `core/config.py` | 六项配置的取值与范围校验 |

## 一次调用回答什么

`POST {base}/v1/systemone`，请求携带 `model`、`state` 与 `questions`，同次请求回答完整性、参与和回复样式的问题，按需追加对象、目标和话题判断：

| 问题 | 类型 | 选项（封闭词表） |
| --- | --- | --- |
| `completion` 是否说完 | choice | `complete` / `wait` |
| `join` 是否开口 | noul | 一个概率值（无置信度） |
| `action` 怎么回 | choice | `ignore` / `acknowledge` / `clarify` / `reply` / `close` |
| `state` 什么状态 | choice | `observing` / `casual` / `focused` / `supportive` / `playful` / `disengaging` |
| `length` 多长 | choice | `brief` / `normal` / `detailed` |
| `reason` 为什么 | choice | `addressed_request` / `addressed_question` / `ongoing_thread` / `open_group_topic` / `social_signal` / `other_recipient` / `boundary_or_sensitive` / `low_value_chatter` |
| `target` 回哪条 | choice | 本轮消息 ID（多于一条候选时才问） |
| `recipient` 实际对象 | choice | `bot` / `other` / `unclear`（引用或昵称唤醒时才问） |

参与与回复样式分别处理：

- **词表是封闭的。** 每个问题只提供插件自己的选项，远程答案无法引入未知的动作、状态、长短或理由；答案按发出的选项键定位，不从散文里解析。回复目标只接受本轮消息——背景消息 ID 等于回答了一个从未问过的问题，此时改用本轮消息本身。
- **普通群聊用动作与参与概率决定是否接话。** 动作置信度与 `jev_min_confidence` 比较，非忽略动作还需通过参与概率门槛；状态、长短和理由的低置信度不否决回复。它们低于同一门槛时分别采用默认状态（回复为 `focused`，收尾为 `disengaging`，旁听为 `observing`）、简短长度和不附加理由的行动目标。理由不确定时记录 `jev_action_accepted`，避免把一个不可靠的分类当作事实。
- **唤醒规则独立。** 真实 @机器人不调用 Jev；引用或昵称唤醒只需实际对象为机器人且对象置信度至少 0.35。获准唤醒后，低置信度动作默认回复，较弱的样式判断同样采用默认值。确认未说完且置信度达标时，仍先等待后续碎发。
- **判断题明确边界。** 任务请求与信息提问分别归类；解决任务和情绪支持按当前主要目的选择；确认、澄清、实质回复和收尾分别给出适用条件。当前消息及账号、引用关系先于人设出现在输入中，背景消息只作上下文，不能成为新的请求。

指令与判据用英语书写（模型自述最强语种），被判断的会话状态原样保留、不翻译；只有交给回复 Agent 的回应目标是中文，与插件其它回应计划一致。

## 凭据与端点

- 凭据只从进程环境按名读取（`jev_api_key_env`，默认 `TYPESAFE_API_KEY`），不写配置、不进面板、不进日志；变量名须含 `JEV`、`TYPESAFE`、`OPENROUTER`、`GATEWAY`、`AIMLAPI` 或 `CHAT_DYNAMICS`，否则回退默认值并提示。
- 只接受 http(s)、无账号密码、无查询串的地址；填写密钥时非本机地址必须 https，否则降级为不调用（`insecure_cleartext`）。
- 地址写法：`https://api.typesafe.ai`（官方，追加 `/v1/systemone`）、`https://openrouter.ai/api`、`https://ai-gateway.vercel.sh/typesafe`（同样追加）；已含完整端点的 `/v1/decisions`（AI/ML API 等）原样使用。
- `jev_model` 默认别名 `jev-latest` 会随新版本移动，调过门槛后建议钉住具体版本；经网关时按各家写法（如 OpenRouter 的 `~typesafe/jev-latest`）。

## 传输契约

- **违反契约的响应整体丢弃。** 选项不在发出的判据里、概率越界、缺置信度、类型不符——任何一处都拒绝整份响应（`invalid_answers`），绝不把可疑答案近似成决策；调用方视为「没有决策」，走本地保守计划。
- **不重试。** 决策有每轮截止时间，重试落在回合之后只是浪费；超时（`jev_timeout`，只包住这一次 HTTP 调用）与网络错误都记为一次失败并回落。
- **重配置作废在途决策。** 改端点、换密钥或关掉后端会取消在途请求，旧端点上的判断不会在新配置下生效。
- `noul` 答案只有概率、没有置信度；`choice` 返回 `choice`/`probabilities`/`confidence`。

## 证据与观测

- 每轮的类型化答案存进 `SessionRuntime.jev_decision`（`confidence` 为动作置信度，各项原始置信度分别保留），随 `model_diagnostic["jev"]` 进入决策诊断。
- 指标：`jev_decision`（咨询到决策）、`jev_unavailable`（无可用答案、回落本地计划）。
- 控制台：会话轨迹标注「Jev 决策 动作置信 x.xx」，直接读取动作字段，兼容旧记录的最弱项汇总值；互联诊断的「决策层」卡片显示模型、调用/失败次数、耗时与置信度门槛。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `decision_backend` | `model` | 决策层后端；`jev` 仅在 `decision_mode=persona_model` 下被咨询 |
| `jev_base_url` | `https://api.typesafe.ai` | Jev 接口地址（见上） |
| `jev_model` | `jev-latest` | Jev 模型 ID |
| `jev_api_key_env` | `TYPESAFE_API_KEY` | 存放密钥的环境变量名（不是密钥本身） |
| `jev_timeout` | `6.0` | 单次决策调用超时秒数（1~30） |
| `jev_min_confidence` | `0.6` | 动作采用门槛（0.3~0.95）；状态、长短和理由低于它时使用默认值 |
