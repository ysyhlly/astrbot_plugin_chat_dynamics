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

`POST {base}/v1/systemone`，请求携带 `model`、`state` 与 `questions`，一次回答五到六个相互独立的问题：

| 问题 | 类型 | 选项（封闭词表） |
| --- | --- | --- |
| `join` 是否开口 | noul | 一个概率值（无置信度） |
| `action` 怎么回 | choice | `ignore` / `acknowledge` / `clarify` / `reply` / `close` |
| `state` 什么状态 | choice | `observing` / `casual` / `focused` / `supportive` / `playful` / `disengaging` |
| `length` 多长 | choice | `brief` / `normal` / `detailed` |
| `reason` 为什么 | choice | `addressed_request` / `addressed_question` / `ongoing_thread` / `open_group_topic` / `social_signal` / `other_recipient` / `boundary_or_sensitive` / `low_value_chatter` |
| `target` 回哪条 | choice | 本轮消息 ID（多于一条候选时才问） |

两条性质是承重的：

- **词表是封闭的。** 每个问题只提供插件自己的选项，远程答案无法引入未知的动作、状态、长短或理由；答案按发出的选项键定位，不从散文里解析。回复目标只接受本轮消息——背景消息 ID 等于回答了一个从未问过的问题，此时改用本轮消息本身。
- **低于门槛什么都不用。** 「方式、状态、长短、理由」四项置信度取最小值，与 `jev_min_confidence` 比较；低于门槛或无法精确映射时，运行本地保守计划：明确请求才回应，环境闲聊继续旁听。理由码带 `jev_` 前缀进入轨迹：`jev_<reason>`、`jev_join_declined`、`jev_low_confidence`、`jev_invalid_answer`、`jev_unavailable`。

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

- 每轮的类型化答案存进 `SessionRuntime.jev_decision`（含四项最弱置信度 `confidence`），随 `model_diagnostic["jev"]` 进入决策诊断；切回聊天模型后端时该字段清空，不留过期证据。
- 指标：`jev_decision`（咨询到决策）、`jev_unavailable`（无可用答案、回落本地计划）。
- 控制台：会话轨迹标注「Jev 决策 置信 x.xx」，互联诊断的「决策层」卡片显示端点、模型与实答模型、调用/失败次数、请求 ID 与置信度门槛。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `decision_backend` | `model` | 决策层后端；`jev` 仅在 `decision_mode=persona_model` 下被咨询 |
| `jev_base_url` | `https://api.typesafe.ai` | Jev 接口地址（见上） |
| `jev_model` | `jev-latest` | Jev 模型 ID |
| `jev_api_key_env` | `TYPESAFE_API_KEY` | 存放密钥的环境变量名（不是密钥本身） |
| `jev_timeout` | `6.0` | 单次决策调用超时秒数（1~30） |
| `jev_min_confidence` | `0.6` | 置信度门槛（0.3~0.95），低于它不用这次判断 |
