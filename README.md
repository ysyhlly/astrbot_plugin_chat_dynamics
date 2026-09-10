<div align="center">

<img src="assets/cover.png" alt="群间 · Chat Dynamics" width="520" />

# 群间 · Chat Dynamics

让机器人跟上群聊节奏，接话有分寸。

</div>

AstrBot 群聊互动插件：合并碎发、追踪话题、判断回应时机，并调整回复节奏。支持沿用当前人设，由模型决定何时参与、回应谁。

## 能做什么

- **等你说完**：合并同一用户的连续消息，减少抢答和重复回复。
- **接对话题**：结合引用、@ 和短期上下文，区分多人交错的讨论。
- **读懂场合**：结合群聊氛围、社交分寸与冷却状态，控制参与程度。
- **自然回应**：按内容调整等待和分段，兼顾闲聊与认真讨论。
- **看得见、管得住**：控制台查看决策原因、会话状态和话题图谱，支持观察、冷却与重置。

### 流式话题归属

模糊消息先保留为待定，不进入正式话题画像；后续明确回复提供独立话题证据时可回填。观察三条后续消息或超过 30 秒仍无法确定，保留为未知。话题画像结合质心、代表消息、最近两轮、回复关系、对话对象、时间与词汇；短句按候选话题提供上下文，引用中的明显话题切换允许另开话题。

沉寂话题使用会话内摘要检索召回原话题 ID，每会话最多 32 条、保留 24 小时，重置或重启后清除。它不替代实时路由，也不依赖外部向量库或较新版本的群消息历史 API。

可选配置 `topic_reranker_enabled`（默认关闭）仅对模糊候选调用 LLM。`topic_reranker_provider` 留空使用当前会话模型，`topic_reranker_timeout` 默认 3 秒；失败或无法确认时继续保留待定。

场景回放中点击话题块，可逐条标注正确归属、新话题或无法判断，并选择合并、拆分、误归属、过早归属、历史召回遗漏等错误类型。标注独立保存，每会话最多 2000 条，可导出 JSON 和混淆计数，不直接修改实时路由。默认不保存消息正文；开启控制台消息内容显示后才保存正文。评分是启发式证据强度，人工样本统计也不等同于真实群聊准确率。

另有媒体门闩、今日作息、群记忆小本，以及 Self Learning / LivingMemory 可选联动。

## 快速开始

环境要求：**AstrBot >= 4.16、< 5，Python 3.12+**。

1. 在 AstrBot 插件管理中使用仓库地址安装：
   ```text
   https://github.com/ysyhlly/astrbot_plugin_chat_dynamics
   ```
2. 在插件配置中填写 `takeover_groups`（生效群 ID）和 `bot_names`（完整昵称）。
3. 保持默认 `decision_mode=legacy`、`pipeline_mode=filter`，先开启 `shadow_mode` 观察。
4. 在测试群用 @、引用和连续碎发检查表现，再关闭 `shadow_mode` 使插件实际介入。

**默认不处理任何群**：白名单为空且 `takeover_all=false` 时不会生效。`exclude_groups` 的优先级高于白名单和全群开关。

## 两种互动模式

| 模式 | 如何决定回应 | 适用场景 |
| --- | --- | --- |
| `legacy`（默认） | 按防抖、指代、氛围与冷却规则处理 | 希望先控制回复时机与节奏 |
| `persona_model` | 模型先判断是否参与及回应对象，再由 AstrBot 主 Agent 回复 | 希望互动更贴合当前人设 |

人设模式沿用当前会话的 AstrBot 人格、历史与工具配置。设置 `decision_mode=persona_model` 即可启用；`decision_provider` 留空时沿用主回复 Provider。未被 @ 的消息也可能触发决策，`ambient_intervention` 不再单独控制是否开口；管理员冷却仍然有效。决策超时或结果无效时，仅明确求助会保守交给 Agent。

观察模式不会改动宿主回复；**人设模式下仍会调用决策模型，产生调用费用**。建议先在一个群观察，再正式启用；切回 `legacy` 可回退。

`pipeline_mode=filter` 是默认过滤链路；`exclusive` 会独占拦截事件，影响后续插件。原生直通和人设模式主 Agent 桥接支持宿主人格、历史与工具，legacy 延迟回复路径不保证完整宿主管线能力。

## 常用配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `takeover_groups` | `[]` | 生效群白名单 |
| `takeover_all` | `false` | 对所有未排除的群生效 |
| `exclude_groups` | `[]` | 排除群黑名单 |
| `bot_names` | `[]` | 机器人完整昵称，避免易误判的短词 |
| `reply_provider` | 空 | 留空依次使用旧 `provider`、当前会话 Provider |
| `shadow_mode` | `false` | 只观察预计行为 |
| `presence_knob` | `sensible` | 分寸旋钮，默认「懂事」 |
| `ambient_intervention` | `false` | legacy 模式允许未被点名时按规则插话 |
| `debounce_base_cooldown` | `3.5` | 连续消息的基础等待秒数 |
| `console_show_message_content` | `false` | 是否在控制台显示截断的消息正文 |

更多参数及范围见插件配置页面或仓库中的 `_conf_schema.json`。氛围 LLM 和神经 Embedding 默认关闭，可按需开启。

## 控制台与指令

在 AstrBot 管理面板打开「群聊动态控制台」，查看活跃会话、决策原因、冷却、防抖积压和对话图谱。页面每 5 秒刷新，消息正文默认脱敏；支持日间／夜间主题。

| 指令 | 权限 | 用途 |
| --- | --- | --- |
| `/dynamics status` | 管理员 | 查看当前会话状态 |
| `/dynamics cool [分钟]` | 管理员 | 开启冷却，指定时长为 1–180 分钟 |
| `/dynamics reset` | 管理员 | 清空当前会话图谱、遥测和冷却，取消待处理输入与在途回复 |
| `/dynamics_stop` | 群成员 | 停止自己当前尚未发送的回复内容 |

停止指令只作用于当前会话和发送者，不撤回已发消息；新请求可直接继续。已经执行的外部工具动作不会随停止撤销。

## 使用边界与联动

- **会话隔离**：状态按完整 UMO（平台、适配器与会话来源）隔离；冷却可跨重载保留。
- **媒体理解**：媒体门闩用于判断是否适合接话，深入理解需相应开关及宿主、适配器、Provider 支持；人设模式可向 Agent 传递附件。
- **今日作息**：模拟群聊中的作息氛围，不提供真实日历或日程管理。
- **记忆联动**：支持 Self Learning / LivingMemory 的宿主钩子及部分分支直连接口。关闭 `selflearning_integration` 只关闭本插件的桥接，其他插件自己的钩子由其自身配置管理。
- **调度兼容**：同一群建议由一个插件负责回复调度，避免与 Group Chat Plus 等插件竞争。

## 开发与文档

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
python -m pytest integration -q  # 需安装真实 AstrBot SDK
python -m ruff check main.py core tests integration scripts
python scripts/check_release.py
```

普通单元测试可能使用 SDK 替身；真实 SDK 契约测试与实际群适配器、媒体和 Dashboard 验收需分别进行。

- [会话路由说明](docs/conversation-router.md)
- [记忆联动说明](docs/companion-integration-fix-2026-09-07.md)
- [直连接口覆盖](docs/selflearning-api-coverage.md)
- [更新记录](CHANGELOG.md) · [MIT License](LICENSE)

插件标识保持 `astrbot_plugin_chat_dynamics`，已有配置与 `/dynamics` 指令继续使用。
